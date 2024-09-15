#importing the logging module
import logging

#now we will create and configure logger
logging.basicConfig(format='%(asctime)s %(message)s')

#let us create a logger object
logger=logging.getLogger()

#now we are going to set the threshold of logger to DEBUG
logger.setLevel(logging.DEBUG)

#messages to test the logging levels
logger.debug("This is a debug message")
logger.info("This is an information message")
logger.warning("This is a warning message")
logger.error("This is an error message")
logger.critical("This is a critical message")
