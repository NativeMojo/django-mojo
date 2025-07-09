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


def run_quick_task(task):
    """Quick task for testing - completes immediately"""
    logger.info("Running quick task with data", task)
    return {"status": "completed", "data": task.data}


def run_slow_task(task):
    """Slow task for testing - takes 10 seconds"""
    logger.info("Running slow task with data", task)
    time.sleep(10)
    return {"status": "completed", "duration": 10}


def run_args_kwargs_task(*args, **kwargs):
    """Task that receives args and kwargs directly"""
    logger.info(f"Running args/kwargs task with args: {args}, kwargs: {kwargs}")
    return {"args": args, "kwargs": kwargs}


def run_data_processing_task(task):
    """Task that processes data and returns results"""
    logger.info("Running data processing task")
    data = task.data
    if not isinstance(data, dict):
        raise ValueError("Data must be a dictionary")
    
    result = {
        "processed": True,
        "input_keys": list(data.keys()),
        "total_items": len(data)
    }
    return result


def run_counter_task(task):
    """Task that increments a counter - for testing state changes"""
    logger.info("Running counter task")
    count = task.data.get("count", 0)
    new_count = count + 1
    logger.info(f"Counter incremented from {count} to {new_count}")
    return {"count": new_count}


def run_timeout_task(task):
    """Task that times out - for testing timeout scenarios"""
    duration = task.data.get("duration", 60)
    logger.info(f"Running timeout task for {duration} seconds")
    time.sleep(duration)
    return {"completed": True}


def run_memory_task(task):
    """Task that uses memory - for testing resource usage"""
    logger.info("Running memory task")
    size = task.data.get("size", 1000000)  # 1MB default
    data = bytearray(size)
    logger.info(f"Allocated {size} bytes")
    return {"allocated_bytes": size}


def run_conditional_error_task(task):
    """Task that conditionally raises an error based on input"""
    logger.info("Running conditional error task")
    should_error = task.data.get("should_error", False)
    error_message = task.data.get("error_message", "Conditional error occurred")
    
    if should_error:
        raise Exception(error_message)
    
    return {"status": "success", "should_error": should_error}


def run_nested_data_task(task):
    """Task that works with nested data structures"""
    logger.info("Running nested data task")
    data = task.data
    
    if "nested" not in data:
        raise ValueError("Missing 'nested' key in data")
    
    nested = data["nested"]
    result = {
        "original": nested,
        "keys": list(nested.keys()) if isinstance(nested, dict) else None,
        "length": len(nested) if hasattr(nested, '__len__') else None
    }
    
    return result


# Test async task handlers
def async_quick_task(message="Hello"):
    """Async task handler for testing decorator"""
    logger.info(f"Async quick task: {message}")
    return f"Processed: {message}"


def async_slow_task(duration=5):
    """Async slow task handler for testing decorator"""
    logger.info(f"Async slow task sleeping for {duration} seconds")
    time.sleep(duration)
    return f"Completed after {duration} seconds"


def async_error_task(should_error=True, message="Async error"):
    """Async error task handler for testing decorator"""
    logger.info(f"Async error task - should_error: {should_error}")
    if should_error:
        raise Exception(message)
    return "No error raised"


def async_args_task(*args, **kwargs):
    """Async task that tests args and kwargs handling"""
    logger.info(f"Async args task - args: {args}, kwargs: {kwargs}")
    return {"received_args": args, "received_kwargs": kwargs}
