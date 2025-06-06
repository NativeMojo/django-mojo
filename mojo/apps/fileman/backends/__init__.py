from typing import Dict, Type
from .base import StorageBackend
from .filesystem import FileSystemStorageBackend
from .s3 import S3StorageBackend


# Registry of available storage backends
BACKEND_REGISTRY: Dict[str, Type[StorageBackend]] = {
    'file': FileSystemStorageBackend,
    's3': S3StorageBackend,
}


def register_backend(backend_type: str, backend_class: Type[StorageBackend]):
    """
    Register a custom storage backend
    
    Args:
        backend_type: Backend type identifier
        backend_class: Backend class that inherits from StorageBackend
    """
    if not issubclass(backend_class, StorageBackend):
        raise ValueError("Backend class must inherit from StorageBackend")
    
    BACKEND_REGISTRY[backend_type] = backend_class


def get_backend(file_manager, **kwargs) -> StorageBackend:
    """
    Get a storage backend instance for the given FileManager
    
    Args:
        file_manager: FileManager instance with backend configuration
        **kwargs: Additional backend-specific configuration
        
    Returns:
        StorageBackend: Configured storage backend instance
        
    Raises:
        ValueError: If backend type is not supported
        Exception: If backend initialization fails
    """
    backend_type = file_manager.backend_type
    
    if backend_type not in BACKEND_REGISTRY:
        available_backends = ', '.join(BACKEND_REGISTRY.keys())
        raise ValueError(
            f"Unsupported backend type '{backend_type}'. "
            f"Available backends: {available_backends}"
        )
    
    backend_class = BACKEND_REGISTRY[backend_type]
    
    try:
        return backend_class(file_manager, **kwargs)
    except Exception as e:
        raise Exception(f"Failed to initialize {backend_type} backend: {e}")


def get_available_backends() -> Dict[str, Type[StorageBackend]]:
    """
    Get all available storage backends
    
    Returns:
        Dict mapping backend type to backend class
    """
    return BACKEND_REGISTRY.copy()


def is_backend_supported(backend_type: str) -> bool:
    """
    Check if a backend type is supported
    
    Args:
        backend_type: Backend type to check
        
    Returns:
        bool: True if backend is supported
    """
    return backend_type in BACKEND_REGISTRY


def validate_backend_config(file_manager) -> tuple[bool, list[str]]:
    """
    Validate backend configuration for a FileManager
    
    Args:
        file_manager: FileManager instance to validate
        
    Returns:
        tuple: (is_valid, list_of_errors)
    """
    try:
        backend = get_backend(file_manager)
        
        # Check if backend has a validate_configuration method
        if hasattr(backend, 'validate_configuration') and callable(getattr(backend, 'validate_configuration')):
            return backend.validate_configuration()
        else:
            # Basic validation - just check if we can create the backend
            return True, []
            
    except Exception as e:
        return False, [str(e)]


__all__ = [
    'StorageBackend',
    'FileSystemStorageBackend',
    'S3StorageBackend',
    'BACKEND_REGISTRY',
    'register_backend',
    'get_backend',
    'get_available_backends',
    'is_backend_supported',
    'validate_backend_config',
]