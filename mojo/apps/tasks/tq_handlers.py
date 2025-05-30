from mojo.helpers import logit
import time

logger = logit.get_logger("ti_example", "ti_example.log")

def run_example_task(task):
    logger.info("Running example task with data", task)
    time.sleep(task.data.get("duration", 5))


def run_error_task(task):
    logger.info("Running error task with data", task)
    time.sleep(2)
    raise Exception("Example error")
