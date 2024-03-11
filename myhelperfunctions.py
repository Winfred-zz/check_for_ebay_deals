import logging
import colorlog
from logging.handlers import RotatingFileHandler
import sys
#======================================================================
# logger
#======================================================================
def logger(log_filepath,logger_name,debug=None):
    """Log plain text to file and to terminal with colors
    
    https://awesomeopensource.com/project/borntyping/python-colorlog?

    use debug=True to turn on debug logging.

    https://docs.python.org/3/howto/logging.html#useful-handlers
    """

    logger = logging.getLogger(logger_name)
    '''guaranteed to have no more than 2 files with a total of 2 MB'''
    logfile_handler = RotatingFileHandler(log_filepath, encoding='utf-8',maxBytes=104857600,backupCount=1)
    plain_formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    logfile_handler.setFormatter(plain_formatter)
    if debug:
        logfile_handler.setLevel(logging.DEBUG) # oh... Does that mean we log all debugs in the logs? hm.
    else:
        logfile_handler.setLevel(logging.INFO) # Let's try this instead. Time to reduce the writes a bit.
    # honestly, I don't think this does anything? I'm not sure...

    # https://docs.python.org/3/library/logging.html look for "LogRecord attributes"
    # https://realpython.com/python-string-formatting/
    # Logging info level to stdout with colors
    terminal_handler = colorlog.StreamHandler()
    color_formatter = colorlog.ColoredFormatter(
        #"%(log_color)s%(levelname)-8s%(reset)s %(asctime)s %(cyan)s%(message)s",
        "%(log_color)s%(asctime)-2s%(reset)s%(cyan)s %(message)s", # theoretically that'll make the date COLOR show if it's an error or info.
        datefmt='%y-%m-%d %H:%M:%S',
        reset=True,
        log_colors={
            'DEBUG':    'blue',
            'INFO':     'green',
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red,bg_white',
        },
        secondary_log_colors={},
        style='%'
    )
    if debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    terminal_handler.setFormatter(color_formatter)

    # Add handlers to logger
    logger.addHandler(logfile_handler)
    logger.addHandler(terminal_handler)
    return logger


my_logger = logger(log_filepath='logs/myhelperfunctions.log', logger_name='myhelperfunctions')

#======================================================================
# sigterm_handler
#======================================================================
# https://stackoverflow.com/questions/9930576/python-what-is-the-default-handling-of-sigterm
def sigterm_handler(_signo, _stack_frame):
    # Raises SystemExit(0):
    my_logger.info('received termination signal')
    sys.exit(0)