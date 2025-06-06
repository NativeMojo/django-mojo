"""
Example configurations for FileManager storage backends

This file contains example configurations for different storage backends
that can be used with the fileman app. Copy and modify these examples
to suit your specific needs.
"""

# Example S3 configurations
S3_CONFIGURATIONS = {
    # Basic S3 configuration
    "s3_basic": {
        "name": "AWS S3 Production",
        "description": "Production S3 storage for file uploads",
        "backend_type": "s3",
        "backend_url": "s3://my-app-files/",
        "supports_direct_upload": True,
        "max_file_size": 100 * 1024 * 1024,  # 100MB
        "allowed_extensions": ["pdf", "doc", "docx", "jpg", "png", "gif"],
        "allowed_mime_types": [
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "image/jpeg",
            "image/png",
            "image/gif"
        ],
        "settings": {
            "bucket_name": "my-app-files",
            "region_name": "us-east-1",
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",  # Use environment variables in production
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # Use environment variables
            "upload_expires_in": 3600,  # 1 hour
            "download_expires_in": 3600,  # 1 hour
            "server_side_encryption": "AES256",  # or "aws:kms"
            "signature_version": "s3v4",
            "addressing_style": "auto"
        }
    },
    
    # S3 with KMS encryption
    "s3_encrypted": {
        "name": "AWS S3 with KMS Encryption",
        "description": "S3 storage with KMS encryption for sensitive files",
        "backend_type": "s3",
        "backend_url": "s3://secure-files/",
        "supports_direct_upload": True,
        "max_file_size": 50 * 1024 * 1024,  # 50MB
        "settings": {
            "bucket_name": "secure-files",
            "region_name": "us-west-2",
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "server_side_encryption": "aws:kms",
            "kms_key_id": "arn:aws:kms:us-west-2:123456789012:key/12345678-1234-1234-1234-123456789012",
            "upload_expires_in": 1800,  # 30 minutes
            "download_expires_in": 300,   # 5 minutes
        }
    },
    
    # S3-compatible service (like MinIO, DigitalOcean Spaces)
    "s3_compatible": {
        "name": "DigitalOcean Spaces",
        "description": "DigitalOcean Spaces storage",
        "backend_type": "s3",
        "backend_url": "https://nyc3.digitaloceanspaces.com/my-space/",
        "supports_direct_upload": True,
        "settings": {
            "bucket_name": "my-space",
            "region_name": "nyc3",
            "endpoint_url": "https://nyc3.digitaloceanspaces.com",
            "access_key_id": "DO00EXAMPLE",
            "secret_access_key": "EXAMPLE_SECRET_KEY",
            "signature_version": "s3v4",
            "addressing_style": "auto"
        }
    }
}

# Example File System configurations
FILESYSTEM_CONFIGURATIONS = {
    # Basic filesystem configuration
    "filesystem_basic": {
        "name": "Local File Storage",
        "description": "Local file system storage for development",
        "backend_type": "file",
        "backend_url": "file:///app/media/uploads/",
        "supports_direct_upload": False,  # Filesystem backend uses custom upload endpoint
        "max_file_size": 10 * 1024 * 1024,  # 10MB
        "settings": {
            "base_path": "/app/media/uploads",
            "base_url": "/media/uploads/",
            "create_directories": True,
            "permissions": 0o644,
            "directory_permissions": 0o755,
            "temp_upload_path": "/app/media/temp",
            "upload_expires_in": 3600
        }
    },
    
    # Filesystem with different organization
    "filesystem_organized": {
        "name": "Organized File Storage",
        "description": "File system storage with date-based organization",
        "backend_type": "file",
        "backend_url": "file:///var/uploads/",
        "supports_direct_upload": False,
        "max_file_size": 100 * 1024 * 1024,  # 100MB
        "allowed_extensions": ["pdf", "doc", "docx", "txt", "csv"],
        "settings": {
            "base_path": "/var/uploads",
            "base_url": "/uploads/",
            "create_directories": True,
            "permissions": 0o644,
            "directory_permissions": 0o755,
            "temp_upload_path": "/var/uploads/temp"
        }
    }
}

# Development configurations
DEVELOPMENT_CONFIGURATIONS = {
    "dev_local": {
        "name": "Development Local Storage",
        "description": "Local development file storage",
        "backend_type": "file",
        "backend_url": "file://./dev_uploads/",
        "supports_direct_upload": False,
        "max_file_size": 50 * 1024 * 1024,  # 50MB
        "is_default": True,
        "settings": {
            "base_path": "./dev_uploads",
            "base_url": "/dev_uploads/",
            "create_directories": True,
            "permissions": 0o644,
            "directory_permissions": 0o755
        }
    },
    
    "dev_s3": {
        "name": "Development S3 (MinIO)",
        "description": "Local MinIO for S3 development testing",
        "backend_type": "s3",
        "backend_url": "s3://dev-bucket/",
        "supports_direct_upload": True,
        "max_file_size": 10 * 1024 * 1024,  # 10MB
        "settings": {
            "bucket_name": "dev-bucket",
            "region_name": "us-east-1",
            "endpoint_url": "http://localhost:9000",  # MinIO default
            "access_key_id": "minioadmin",
            "secret_access_key": "minioadmin",
            "signature_version": "s3v4"
        }
    }
}

# Production configurations
PRODUCTION_CONFIGURATIONS = {
    "prod_s3_primary": {
        "name": "Production S3 Primary",
        "description": "Primary production S3 storage",
        "backend_type": "s3", 
        "backend_url": "s3://prod-files-primary/",
        "supports_direct_upload": True,
        "max_file_size": 500 * 1024 * 1024,  # 500MB
        "is_default": True,
        "allowed_extensions": [
            "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
            "jpg", "jpeg", "png", "gif", "svg", "webp",
            "mp4", "webm", "avi", "mov",
            "zip", "tar", "gz", "rar"
        ],
        "settings": {
            "bucket_name": "prod-files-primary",
            "region_name": "us-east-1",
            "access_key_id": "${AWS_ACCESS_KEY_ID}",  # Use environment variables
            "secret_access_key": "${AWS_SECRET_ACCESS_KEY}",
            "server_side_encryption": "AES256",
            "upload_expires_in": 7200,  # 2 hours
            "download_expires_in": 3600,  # 1 hour
            "multipart_threshold": 8 * 1024 * 1024,  # 8MB
            "max_concurrency": 10
        }
    },
    
    "prod_s3_backup": {
        "name": "Production S3 Backup",
        "description": "Backup S3 storage in different region",
        "backend_type": "s3",
        "backend_url": "s3://prod-files-backup/",
        "supports_direct_upload": True,
        "max_file_size": 500 * 1024 * 1024,  # 500MB
        "settings": {
            "bucket_name": "prod-files-backup",
            "region_name": "us-west-2",  # Different region for redundancy
            "access_key_id": "${AWS_ACCESS_KEY_ID}",
            "secret_access_key": "${AWS_SECRET_ACCESS_KEY}",
            "server_side_encryption": "AES256"
        }
    }
}

# Specialized configurations
SPECIALIZED_CONFIGURATIONS = {
    "images_only": {
        "name": "Images Only Storage",
        "description": "Storage specifically for image files",
        "backend_type": "s3",
        "backend_url": "s3://app-images/",
        "supports_direct_upload": True,
        "max_file_size": 20 * 1024 * 1024,  # 20MB
        "allowed_extensions": ["jpg", "jpeg", "png", "gif", "webp", "svg"],
        "allowed_mime_types": [
            "image/jpeg",
            "image/png", 
            "image/gif",
            "image/webp",
            "image/svg+xml"
        ],
        "settings": {
            "bucket_name": "app-images",
            "region_name": "us-east-1",
            "access_key_id": "${AWS_ACCESS_KEY_ID}",
            "secret_access_key": "${AWS_SECRET_ACCESS_KEY}",
            "upload_expires_in": 1800,  # 30 minutes
            "download_expires_in": 86400  # 24 hours (for CDN caching)
        }
    },
    
    "documents_secure": {
        "name": "Secure Documents",
        "description": "Encrypted storage for sensitive documents",
        "backend_type": "s3",
        "backend_url": "s3://secure-docs/",
        "supports_direct_upload": True,
        "max_file_size": 100 * 1024 * 1024,  # 100MB
        "allowed_extensions": ["pdf", "doc", "docx"],
        "allowed_mime_types": [
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ],
        "is_public": False,
        "settings": {
            "bucket_name": "secure-docs",
            "region_name": "us-east-1", 
            "access_key_id": "${AWS_ACCESS_KEY_ID}",
            "secret_access_key": "${AWS_SECRET_ACCESS_KEY}",
            "server_side_encryption": "aws:kms",
            "kms_key_id": "${KMS_KEY_ID}",
            "upload_expires_in": 600,   # 10 minutes
            "download_expires_in": 300  # 5 minutes
        }
    },
    
    "temp_uploads": {
        "name": "Temporary Uploads",
        "description": "Short-term storage for temporary files",
        "backend_type": "file",
        "backend_url": "file:///tmp/uploads/",
        "supports_direct_upload": False,
        "max_file_size": 50 * 1024 * 1024,  # 50MB
        "settings": {
            "base_path": "/tmp/uploads",
            "base_url": "/temp/",
            "create_directories": True,
            "upload_expires_in": 1800,  # 30 minutes
            "permissions": 0o600,  # More restrictive permissions
            "directory_permissions": 0o700
        }
    }
}

# Environment-based configuration function
def get_configuration_for_environment(env="development"):
    """
    Get appropriate configuration based on environment
    
    Args:
        env: Environment name ("development", "staging", "production")
        
    Returns:
        dict: Configuration dictionary
    """
    if env == "development":
        return DEVELOPMENT_CONFIGURATIONS["dev_local"]
    elif env == "staging":
        # Use production-like S3 but with smaller limits
        config = PRODUCTION_CONFIGURATIONS["prod_s3_primary"].copy()
        config["name"] = "Staging S3 Storage"
        config["description"] = "Staging environment S3 storage"
        config["max_file_size"] = 100 * 1024 * 1024  # 100MB instead of 500MB
        config["settings"]["bucket_name"] = "staging-files"
        return config
    elif env == "production":
        return PRODUCTION_CONFIGURATIONS["prod_s3_primary"]
    else:
        raise ValueError(f"Unknown environment: {env}")

# Usage examples for Django management commands or admin
"""
# Example: Create FileManager instances from configurations

from mojo.apps.fileman.models import FileManager
from mojo.apps.account.models import Group

# Get or create a group
group, created = Group.objects.get_or_create(name="Default Group")

# Create a file manager from configuration
config = PRODUCTION_CONFIGURATIONS["prod_s3_primary"]
file_manager = FileManager.objects.create(
    group=group,
    name=config["name"],
    description=config["description"],
    backend_type=config["backend_type"],
    backend_url=config["backend_url"],
    supports_direct_upload=config["supports_direct_upload"],
    max_file_size=config["max_file_size"],
    allowed_extensions=config.get("allowed_extensions", []),
    allowed_mime_types=config.get("allowed_mime_types", []),
    settings=config["settings"],
    is_default=config.get("is_default", False),
    is_active=True
)
"""

# Environment variables that should be set in production
REQUIRED_ENVIRONMENT_VARIABLES = {
    "s3": [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",  # Optional but recommended
        "KMS_KEY_ID"  # If using KMS encryption
    ],
    "file": [
        "UPLOAD_ROOT_PATH",  # Base path for file uploads
        "MEDIA_URL"  # URL prefix for serving files
    ]
}

# Security recommendations
SECURITY_RECOMMENDATIONS = """
Security Best Practices for File Management:

1. S3 Configuration:
   - Never hardcode AWS credentials in your code
   - Use IAM roles when running on EC2/ECS
   - Use environment variables or AWS Secrets Manager
   - Enable bucket versioning and lifecycle policies
   - Use least-privilege IAM policies
   - Consider enabling MFA delete for critical buckets

2. File Validation:
   - Always validate file extensions and MIME types
   - Set reasonable file size limits
   - Scan files for malware if accepting user uploads
   - Use Content Security Policy headers when serving files

3. Access Control:
   - Implement proper authentication and authorization
   - Use signed URLs with appropriate expiration times
   - Consider IP restrictions for sensitive files
   - Log all file access attempts

4. Data Protection:
   - Use encryption at rest (S3 server-side encryption)
   - Use encryption in transit (HTTPS)
   - Consider client-side encryption for highly sensitive data
   - Implement proper backup and disaster recovery

5. Monitoring:
   - Monitor upload/download patterns
   - Set up alerts for unusual activity
   - Track storage costs and usage
   - Log all file operations for audit trails
"""