import importlib
import pkgutil
import sys


def module_exists(module_name):
    return pkgutil.find_loader(module_name)


def load_module_to_globals(module, memory=None):
    """
    Injects all uppercase variables from the given module into the specified namespace.

    Args:
        module: The module whose variables should be injected.
        memory (dict, optional): The namespace where variables will be stored.
                                 Defaults to `globals()`.
    """
    if memory is None:
        memory = globals()

    # Extract only variables that are ALL CAPS (Django convention for settings)
    memory.update({key: value for key, value in vars(module).items() if key.isupper()})


def load_module(module_name, package=__name__, ignore_errors=True):
    """
    Import a module by name and inject its variables into the global namespace.

    Args:
        module_name (str): The name of the module to be imported.
        package (str, optional): The package name to use for relative imports. Defaults to the current package.

    Raises:
        ImportError: If the module cannot be found in the specified package.
    """
    if ignore_errors:
        try:
            module = importlib.import_module(module_name, package=package)
            return module
        except ModuleNotFoundError:
            pass
        return None
    return importlib.import_module(module_name, package=package)

def get_root_module(func):
    """
    Get the root (top-level) module of a function.

    :param func: The function to inspect.
    :return: The root module name (str) or None if not found.
    """
    if not hasattr(func, "__module__"):
        return None  # Not a valid function or method

    # Fully unwrap to get the original function
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__

    module_name = func.__module__  # Get the module where the function is defined

    if module_name not in sys.modules:
        return None  # The module is not loaded

    # Extract the root module (top-level package)
    root_module = module_name.split('.')[0]

    return sys.modules.get(root_module, None)  # Return the module object or None


def get_model(app_name, model_name):
    # Import the module containing the models
    models_module = importlib.import_module(f"{app_name}.models")
    # Get the model class from the module
    model = getattr(models_module, model_name)
    return model

def get_model_instance(app_name, model_name, pk):
    return get_model(app_name, model_name).objects.filter(id=pk).last()
