"""
Logger implementation loosely modeled on PEP 282.  We don't use the
PEP 282 logger implementation in the stdlib ('logging') because it's
idiosyncratic and a bit slow for our purposes (we don't use threads).
"""

# This module must not depend on any non-stdlib modules to
# avoid circular import problems

import re
import os
import errno
import sys
import time
import traceback

from supervisor.compat import syslog
from supervisor.compat import long
from supervisor.compat import is_text_stream
from supervisor.compat import as_string

_MIDNIGHT = 24 * 60 * 60  # number of seconds in a day

class LevelsByName:
    CRIT = 50   # messages that probably require immediate user attention
    ERRO = 40   # messages that indicate a potentially ignorable error condition
    WARN = 30   # messages that indicate issues which aren't errors
    INFO = 20   # normal informational output
    DEBG = 10   # messages useful for users trying to debug configurations
    TRAC = 5    # messages useful to developers trying to debug plugins
    BLAT = 3    # messages useful for developers trying to debug supervisor

class LevelsByDescription:
    critical = LevelsByName.CRIT
    error = LevelsByName.ERRO
    warn = LevelsByName.WARN
    info = LevelsByName.INFO
    debug = LevelsByName.DEBG
    trace = LevelsByName.TRAC
    blather = LevelsByName.BLAT

def _levelNumbers():
    bynumber = {}
    for name, number in LevelsByName.__dict__.items():
        if not name.startswith('_'):
            bynumber[number] = name
    return bynumber

LOG_LEVELS_BY_NUM = _levelNumbers()

def getLevelNumByDescription(description):
    num = getattr(LevelsByDescription, description, None)
    return num

class Handler:
    fmt = '%(message)s'
    level = LevelsByName.INFO

    def __init__(self, stream=None):
        self.stream = stream
        self.closed = False

    def setFormat(self, fmt):
        self.fmt = fmt

    def setLevel(self, level):
        self.level = level

    def flush(self):
        try:
            self.stream.flush()
        except IOError as why:
            # if supervisor output is piped, EPIPE can be raised at exit
            if why.args[0] != errno.EPIPE:
                raise

    def close(self):
        if not self.closed:
            if hasattr(self.stream, 'fileno'):
                try:
                    fd = self.stream.fileno()
                except IOError:
                    # on python 3, io.IOBase objects always have fileno()
                    # but calling it may raise io.UnsupportedOperation
                    pass
                else:
                    if fd < 3: # don't ever close stdout or stderr
                        return
            self.stream.close()
            self.closed = True

    def emit(self, record):
        try:
            binary = (self.fmt == '%(message)s' and
                      isinstance(record.msg, bytes) and
                      (not record.kw or record.kw == {'exc_info': None}))
            binary_stream = not is_text_stream(self.stream)
            if binary:
                msg = record.msg
            else:
                msg = self.fmt % record.asdict()
                if binary_stream:
                    msg = msg.encode('utf-8')
            try:
                self.stream.write(msg)
            except TypeError:
                self.stream.write(msg.decode('utf-8'))
            except UnicodeError:
                # TODO sort out later
                # this only occurs because of a test stream type
                # which deliberately raises an exception the first
                # time it's called. So just do it again
                self.stream.write(msg)
            self.flush()
        except:
            self.handleError()

    def handleError(self):
        ei = sys.exc_info()
        traceback.print_exception(ei[0], ei[1], ei[2], None, sys.stderr)
        del ei

class StreamHandler(Handler):
    def __init__(self, strm=None):
        Handler.__init__(self, strm)

    def remove(self):
        if hasattr(self.stream, 'clear'):
            self.stream.clear()

    def reopen(self):
        pass

class BoundIO:
    def __init__(self, maxbytes, buf=b''):
        self.maxbytes = maxbytes
        self.buf = buf

    def flush(self):
        pass

    def close(self):
        self.clear()

    def write(self, b):
        blen = len(b)
        if len(self.buf) + blen > self.maxbytes:
            self.buf = self.buf[blen:]
        self.buf += b

    def getvalue(self):
        return self.buf

    def clear(self):
        self.buf = b''

class FileHandler(Handler):
    """File handler which supports reopening of logs.
    """

    def __init__(self, filename, mode='ab'):
        Handler.__init__(self)

        try:
            self.stream = open(filename, mode)
        except OSError as e:
            if mode == 'ab' and e.errno == errno.ESPIPE:
                # Python 3 can't open special files like
                # /dev/stdout in 'a' mode due to an implicit seek call
                # that fails with ESPIPE. Retry in 'w' mode.
                # See: http://bugs.python.org/issue27805
                mode = 'wb'
                self.stream = open(filename, mode)
            else:
                raise

        self.baseFilename = filename
        self.mode = mode

    def reopen(self):
        self.close()
        self.stream = open(self.baseFilename, self.mode)
        self.closed = False

    def remove(self):
        self.close()
        try:
            os.remove(self.baseFilename)
        except OSError as why:
            if why.args[0] != errno.ENOENT:
                raise

class RotatingFileHandler(FileHandler):
    def __init__(self, filename, mode='ab', maxBytes=512*1024*1024,
                 backupCount=10):
        """
        Open the specified file and use it as the stream for logging.

        By default, the file grows indefinitely. You can specify particular
        values of maxBytes and backupCount to allow the file to rollover at
        a predetermined size.

        Rollover occurs whenever the current log file is nearly maxBytes in
        length. If backupCount is >= 1, the system will successively create
        new files with the same pathname as the base file, but with extensions
        ".1", ".2" etc. appended to it. For example, with a backupCount of 5
        and a base file name of "app.log", you would get "app.log",
        "app.log.1", "app.log.2", ... through to "app.log.5". The file being
        written to is always "app.log" - when it gets filled up, it is closed
        and renamed to "app.log.1", and if files "app.log.1", "app.log.2" etc.
        exist, then they are renamed to "app.log.2", "app.log.3" etc.
        respectively.

        If maxBytes is zero, rollover never occurs.
        """
        if maxBytes > 0:
            mode = 'ab' # doesn't make sense otherwise!
        FileHandler.__init__(self, filename, mode)
        self.maxBytes = maxBytes
        self.backupCount = backupCount
        self.counter = 0
        self.every = 10

    def emit(self, record):
        """
        Emit a record.

        Output the record to the file, catering for rollover as described
        in doRollover().
        """
        FileHandler.emit(self, record)
        self.doRollover()

    def _remove(self, fn): # pragma: no cover
        # this is here to service stubbing in unit tests
        return os.remove(fn)

    def _rename(self, src, tgt): # pragma: no cover
        # this is here to service stubbing in unit tests
        return os.rename(src, tgt)

    def _exists(self, fn): # pragma: no cover
        # this is here to service stubbing in unit tests
        return os.path.exists(fn)

    def removeAndRename(self, sfn, dfn):
        if self._exists(dfn):
            try:
                self._remove(dfn)
            except OSError as why:
                # catch race condition (destination already deleted)
                if why.args[0] != errno.ENOENT:
                    raise
        try:
            self._rename(sfn, dfn)
        except OSError as why:
            # catch exceptional condition (source deleted)
            # E.g. cleanup script removes active log.
            if why.args[0] != errno.ENOENT:
                raise

    def doRollover(self):
        """
        Do a rollover, as described in __init__().
        """
        if self.maxBytes <= 0:
            return

        if not (self.stream.tell() >= self.maxBytes):
            return

        self.stream.close()
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                sfn = "%s.%d" % (self.baseFilename, i)
                dfn = "%s.%d" % (self.baseFilename, i + 1)
                if os.path.exists(sfn):
                    self.removeAndRename(sfn, dfn)
            dfn = self.baseFilename + ".1"
            self.removeAndRename(self.baseFilename, dfn)
        self.stream = open(self.baseFilename, 'wb')

class TimedRotatingFileHandler(FileHandler):

    def __init__(self, filename, 
                 when='h', interval=1, backupCount=0, mode='ab',
                 utc=False, atTime=None):
        """
        """
        FileHandler.__init__(self, filename, mode)
        self.when = when.upper()
        self.backupCount = backupCount
        self.utc = utc
        self.atTime = atTime
        # Calculate the real rollover interval, which is just the number of
        # seconds between rollovers.  Also set the filename suffix used when
        # a rollover occurs.  Current 'when' events supported:
        # S - Seconds
        # M - Minutes
        # H - Hours
        # D - Days
        # midnight - roll over at midnight
        # W{0-6} - roll over on a certain day; 0 - Monday
        #
        # Case of the 'when' specifier is not important; lower or upper case
        # will work.
        if self.when == 'S':
            self.interval = 1 # one second
            self.suffix = "%Y-%m-%d_%H-%M-%S"
            extMatch = r"(?<!\d)\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?!\d)"
        elif self.when == 'M':
            self.interval = 60 # one minute
            self.suffix = "%Y-%m-%d_%H-%M"
            extMatch = r"(?<!\d)\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(?!\d)"
        elif self.when == 'H':
            self.interval = 60 * 60 # one hour
            self.suffix = "%Y-%m-%d_%H"
            extMatch = r"(?<!\d)\d{4}-\d{2}-\d{2}_\d{2}(?!\d)"
        elif self.when == 'D' or self.when == 'MIDNIGHT':
            self.interval = 60 * 60 * 24 # one day
            self.suffix = "%Y-%m-%d"
            extMatch = r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"
        elif self.when.startswith('W'):
            self.interval = 60 * 60 * 24 * 7 # one week
            if len(self.when) != 2:
                raise ValueError("You must specify a day for weekly rollover from 0 to 6 (0 is Monday): %s" % self.when)
            if self.when[1] < '0' or self.when[1] > '6':
                raise ValueError("Invalid day specified for weekly rollover: %s" % self.when)
            self.dayOfWeek = int(self.when[1])
            self.suffix = "%Y-%m-%d"
            extMatch = r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"
        else:
            raise ValueError("Invalid rollover interval specified: %s" % self.when)

        # extMatch is a pattern for matching a datetime suffix in a file name.
        # After custom naming, it is no longer guaranteed to be separated by
        # periods from other parts of the filename.  The lookup statements
        # (?<!\d) and (?!\d) ensure that the datetime suffix (which itself
        # starts and ends with digits) is not preceded or followed by digits.
        # This reduces the number of false matches and improves performance.
        self.extMatch = re.compile(extMatch, re.ASCII)
        self.interval = self.interval * interval # multiply by units requested
        # The following line added because the filename passed in could be a
        # path object (see Issue #27493), but self.baseFilename will be a string
        filename = self.baseFilename
        if os.path.exists(filename):
            t = int(os.stat(filename).st_mtime)
        else:
            t = int(time.time())
        self.rolloverAt = self.computeRollover(t)

    def computeRollover(self, currentTime):
        """
        Work out the rollover time based on the specified time.
        """
        result = currentTime + self.interval
        # If we are rolling over at midnight or weekly, then the interval is already known.
        # What we need to figure out is WHEN the next interval is.  In other words,
        # if you are rolling over at midnight, then your base interval is 1 day,
        # but you want to start that one day clock at midnight, not now.  So, we
        # have to fudge the rolloverAt value in order to trigger the first rollover
        # at the right time.  After that, the regular interval will take care of
        # the rest.  Note that this code doesn't care about leap seconds. :)
        if self.when == 'MIDNIGHT' or self.when.startswith('W'):
            # This could be done with less code, but I wanted it to be clear
            if self.utc:
                t = time.gmtime(currentTime)
            else:
                t = time.localtime(currentTime)
            currentHour = t[3]
            currentMinute = t[4]
            currentSecond = t[5]
            currentDay = t[6]
            # r is the number of seconds left between now and the next rotation
            if self.atTime is None:
                rotate_ts = _MIDNIGHT
            else:
                rotate_ts = ((self.atTime.hour * 60 + self.atTime.minute)*60 +
                    self.atTime.second)

            r = rotate_ts - ((currentHour * 60 + currentMinute) * 60 +
                currentSecond)
            if r <= 0:
                # Rotate time is before the current time (for example when
                # self.rotateAt is 13:45 and it now 14:15), rotation is
                # tomorrow.
                r += _MIDNIGHT
                currentDay = (currentDay + 1) % 7
            result = currentTime + r
            # If we are rolling over on a certain day, add in the number of days until
            # the next rollover, but offset by 1 since we just calculated the time
            # until the next day starts.  There are three cases:
            # Case 1) The day to rollover is today; in this case, do nothing
            # Case 2) The day to rollover is further in the interval (i.e., today is
            #         day 2 (Wednesday) and rollover is on day 6 (Sunday).  Days to
            #         next rollover is simply 6 - 2 - 1, or 3.
            # Case 3) The day to rollover is behind us in the interval (i.e., today
            #         is day 5 (Saturday) and rollover is on day 3 (Thursday).
            #         Days to rollover is 6 - 5 + 3, or 4.  In this case, it's the
            #         number of days left in the current week (1) plus the number
            #         of days in the next week until the rollover day (3).
            # The calculations described in 2) and 3) above need to have a day added.
            # This is because the above time calculation takes us to midnight on this
            # day, i.e. the start of the next day.
            if self.when.startswith('W'):
                day = currentDay # 0 is Monday
                if day != self.dayOfWeek:
                    if day < self.dayOfWeek:
                        daysToWait = self.dayOfWeek - day
                    else:
                        daysToWait = 6 - day + self.dayOfWeek + 1
                    result += daysToWait * _MIDNIGHT
                result += self.interval - _MIDNIGHT * 7
            else:
                result += self.interval - _MIDNIGHT
            if not self.utc:
                dstNow = t[-1]
                dstAtRollover = time.localtime(result)[-1]
                if dstNow != dstAtRollover:
                    if not dstNow:  # DST kicks in before next rollover, so we need to deduct an hour
                        addend = -3600
                        if not time.localtime(result-3600)[-1]:
                            addend = 0
                    else:           # DST bows out before next rollover, so we need to add an hour
                        addend = 3600
                    result += addend
        return result
    
    def shouldRollover(self, record):
        """
        Determine if rollover should occur.

        record is not used, as we are just comparing times, but it is needed so
        the method signatures are the same
        """
        t = int(time.time())
        if t >= self.rolloverAt:
            # See #89564: Never rollover anything other than regular files
            if os.path.exists(self.baseFilename) and not os.path.isfile(self.baseFilename):
                # The file is not a regular file, so do not rollover, but do
                # set the next rollover time to avoid repeated checks.
                self.rolloverAt = self.computeRollover(t)
                return False

            return True
        return False

    def emit(self, record):
        """
        Emit a record.

        Output the record to the file, catering for rollover as described
        in doRollover().
        """
        if self.shouldRollover(record):
            self.doRollover()
        FileHandler.emit(self, record)
        
    def _remove(self, fn): # pragma: no cover
        # this is here to service stubbing in unit tests
        return os.remove(fn)

    def _rename(self, src, tgt): # pragma: no cover
        # this is here to service stubbing in unit tests
        return os.rename(src, tgt)

    def _exists(self, fn): # pragma: no cover
        # this is here to service stubbing in unit tests
        return os.path.exists(fn)

    def removeAndRename(self, sfn, dfn):
        if self._exists(dfn):
            try:
                self._remove(dfn)
            except OSError as why:
                # catch race condition (destination already deleted)
                if why.args[0] != errno.ENOENT:
                    raise
        try:
            self._rename(sfn, dfn)
        except OSError as why:
            # catch exceptional condition (source deleted)
            # E.g. cleanup script removes active log.
            if why.args[0] != errno.ENOENT:
                raise

    def getFilesToDelete(self):
        """
        Determine the files to delete when rolling over.

        More specific than the earlier method, which just used glob.glob().
        """
        dirName, baseName = os.path.split(self.baseFilename)
        fileNames = os.listdir(dirName or '.')
        result = []
        self.namer = None
        if self.namer is None:
            prefix = baseName + '.'
            plen = len(prefix)
            for fileName in fileNames:
                if fileName[:plen] == prefix:
                    suffix = fileName[plen:]
                    if self.extMatch.fullmatch(suffix):
                        result.append(os.path.join(dirName, fileName))
        else:
            for fileName in fileNames:
                # Our files could be just about anything after custom naming,
                # but they should contain the datetime suffix.
                # Try to find the datetime suffix in the file name and verify
                # that the file name can be generated by this handler.
                m = self.extMatch.search(fileName)
                while m:
                    dfn = self.namer(self.baseFilename + "." + m[0])
                    if os.path.basename(dfn) == fileName:
                        result.append(os.path.join(dirName, fileName))
                        break
                    m = self.extMatch.search(fileName, m.start() + 1)

        if len(result) < self.backupCount:
            result = []
        else:
            result.sort()
            result = result[:len(result) - self.backupCount]
        return result
    
    def doRollover(self):
        """
        do a rollover; in this case, a date/time stamp is appended to the filename
        when the rollover happens.  However, you want the file to be named for the
        start of the interval, not the current time.  If there is a backup count,
        then we have to get a list of matching filenames, sort them and remove
        the one with the oldest suffix.
        """
        # get the time that this sequence started at and make it a TimeTuple
        currentTime = int(time.time())
        t = self.rolloverAt - self.interval
        if self.utc:
            timeTuple = time.gmtime(t)
        else:
            timeTuple = time.localtime(t)
            dstNow = time.localtime(currentTime)[-1]
            dstThen = timeTuple[-1]
            if dstNow != dstThen:
                if dstNow:
                    addend = 3600
                else:
                    addend = -3600
                timeTuple = time.localtime(t + addend)
        dfn = self.baseFilename + "." + time.strftime(self.suffix, timeTuple)

        if os.path.exists(dfn):
            # Already rolled over.
            return
        
        self.stream.close()
        self.removeAndRename(self.baseFilename, dfn)

        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                if os.path.exists(s):
                    self._remove(s)

        self.stream = open(self.baseFilename, 'wb')
        self.rolloverAt = self.computeRollover(currentTime)

class LogRecord:
    def __init__(self, level, msg, **kw):
        self.level = level
        self.msg = msg
        self.kw = kw
        self.dictrepr = None

    def asdict(self):
        if self.dictrepr is None:
            now = time.time()
            msecs = (now - long(now)) * 1000
            part1 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            asctime = '%s,%03d' % (part1, msecs)
            levelname = LOG_LEVELS_BY_NUM[self.level]
            msg = as_string(self.msg)
            if self.kw:
                msg = msg % self.kw
            self.dictrepr = {'message':msg, 'levelname':levelname,
                             'asctime':asctime}
        return self.dictrepr

class Logger:
    def __init__(self, level=None, handlers=None):
        if level is None:
            level = LevelsByName.INFO
        self.level = level

        if handlers is None:
            handlers = []
        self.handlers = handlers

    def close(self):
        for handler in self.handlers:
            handler.close()

    def blather(self, msg, **kw):
        if LevelsByName.BLAT >= self.level:
            self.log(LevelsByName.BLAT, msg, **kw)

    def trace(self, msg, **kw):
        if LevelsByName.TRAC >= self.level:
            self.log(LevelsByName.TRAC, msg, **kw)

    def debug(self, msg, **kw):
        if LevelsByName.DEBG >= self.level:
            self.log(LevelsByName.DEBG, msg, **kw)

    def info(self, msg, **kw):
        if LevelsByName.INFO >= self.level:
            self.log(LevelsByName.INFO, msg, **kw)

    def warn(self, msg, **kw):
        if LevelsByName.WARN >= self.level:
            self.log(LevelsByName.WARN, msg, **kw)

    def error(self, msg, **kw):
        if LevelsByName.ERRO >= self.level:
            self.log(LevelsByName.ERRO, msg, **kw)

    def critical(self, msg, **kw):
        if LevelsByName.CRIT >= self.level:
            self.log(LevelsByName.CRIT, msg, **kw)

    def log(self, level, msg, **kw):
        record = LogRecord(level, msg, **kw)
        for handler in self.handlers:
            if level >= handler.level:
                handler.emit(record)

    def addHandler(self, hdlr):
        self.handlers.append(hdlr)

    def getvalue(self):
        raise NotImplementedError

class SyslogHandler(Handler):
    def __init__(self):
        Handler.__init__(self)
        assert syslog is not None, "Syslog module not present"

    def close(self):
        pass

    def reopen(self):
        pass

    def _syslog(self, msg): # pragma: no cover
        # this exists only for unit test stubbing
        syslog.syslog(msg)

    def emit(self, record):
        try:
            params = record.asdict()
            message = params['message']
            for line in message.rstrip('\n').split('\n'):
                params['message'] = line
                msg = self.fmt % params
                try:
                    self._syslog(msg)
                except UnicodeError:
                    self._syslog(msg.encode("UTF-8"))
        except:
            self.handleError()

def getLogger(level=None):
    return Logger(level)

_2MB = 1<<21

def handle_boundIO(logger, fmt, maxbytes=_2MB):
    """Attach a new BoundIO handler to an existing Logger"""
    io = BoundIO(maxbytes)
    handler = StreamHandler(io)
    handler.setLevel(logger.level)
    handler.setFormat(fmt)
    logger.addHandler(handler)
    logger.getvalue = io.getvalue

def handle_stdout(logger, fmt):
    """Attach a new StreamHandler with stdout handler to an existing Logger"""
    handler = StreamHandler(sys.stdout)
    handler.setFormat(fmt)
    handler.setLevel(logger.level)
    logger.addHandler(handler)

def handle_syslog(logger, fmt):
    """Attach a new Syslog handler to an existing Logger"""
    handler = SyslogHandler()
    handler.setFormat(fmt)
    handler.setLevel(logger.level)
    logger.addHandler(handler)

def handle_file(logger, filename, fmt, rotating=False, maxbytes=0, backups=0):
    """Attach a new file handler to an existing Logger. If the filename
    is the magic name of 'syslog' then make it a syslog handler instead."""
    if filename == 'syslog': # TODO remove this
        handler = SyslogHandler()
    else:
        if rotating is False:
            handler = FileHandler(filename)
        else:
            handler = TimedRotatingFileHandler(filename, when='s', interval=30, backupCount=backups, mode='a', utc=False, atTime=None)
    handler.setFormat(fmt)
    handler.setLevel(logger.level)
    logger.addHandler(handler)
