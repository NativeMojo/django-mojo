# Django File Manager (fileman)

A comprehensive file management system for Django that supports multiple storage backends with direct upload capabilities to services like AWS S3, avoiding server bottlenecks.

## Features

- **Multiple Storage Backends**: File system, AWS S3, Azure Blob, Google Cloud Storage
- **Direct Upload Support**: Generate pre-signed URLs for client-side uploads to cloud services
- **Flexible Configuration**: Per-group file managers with different backends and restrictions
- **File Validation**: Size limits, extension filtering, MIME type validation
- **Upload Lifecycle Management**: Track upload status from initiation to completion
- **Automatic Cleanup**: Management commands for cleaning expired uploads
- **Security**: Token-based uploads, access controls, encryption support

## Quick Start

### 1. Add to Django Settings

```python
INSTALLED_APPS = [
    # ... other apps
    'mojo.apps.fileman',
]

# Optional: Configure default settings
FILEMAN_SETTINGS = {
    'DEFAULT_MAX_FILE_SIZE': 100 * 1024 * 1024,  # 100MB
    'DEFAULT_UPLOAD_EXPIRES_IN': 3600,  # 1 hour
    'CLEANUP_EXPIRED_UPLOADS': True,
}
```

### 2. Run Migrations

```bash
python manage.py migrate fileman
```

### 3. Create a FileManager

```python
from mojo.apps.fileman.models import FileManager
from mojo.apps.account.models import Group

# Create a file manager for S3
file_manager = FileManager.objects.create(
    name="AWS S3 Storage",
    backend_type="s3",
    backend_url="s3://my-bucket/",
    supports_direct_upload=True,
    max_file_size=100 * 1024 * 1024,  # 100MB
    settings={
        "bucket_name": "my-bucket",
        "region_name": "us-east-1",
        "access_key_id": "YOUR_ACCESS_KEY",
        "secret_access_key": "YOUR_SECRET_KEY",
    },
    is_default=True,
    is_active=True
)
```

## API Usage

### 1. Initiate Upload

**POST** `/fileman/initiate-upload/`

```javascript
const response = await fetch('/fileman/initiate-upload/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify({
        files: [
            {
                filename: 'document.pdf',
                content_type: 'application/pdf',
                size: 1024000
            },
            {
                filename: 'image.jpg',
                content_type: 'image/jpeg',
                size: 512000
            }
        ],
        metadata: {
            source: 'web_upload',
            category: 'documents'
        }
    })
});

const data = await response.json();
```

**Response:**
```json
{
    "success": true,
    "files": [
        {
            "id": 123,
            "storage_filename": "document_20231201_abc12345.pdf",
            "filename": "document.pdf",
            "upload_token": "a1b2c3d4e5f6...",
            "upload_url": "https://s3.amazonaws.com/my-bucket/...",
            "method": "POST",
            "fields": {
                "key": "uploads/document_20231201_abc12345.pdf",
                "policy": "eyJ...",
                "x-amz-algorithm": "AWS4-HMAC-SHA256",
                "x-amz-credential": "...",
                "x-amz-date": "20231201T120000Z",
                "x-amz-signature": "..."
            },
            "expires_at": "2023-12-01T13:00:00Z"
        }
    ],
    "file_manager": {
        "id": 1,
        "name": "AWS S3 Storage",
        "backend_type": "s3"
    }
}
```

### 2. Upload File to Pre-signed URL

```javascript
// For S3 direct upload
const formData = new FormData();

// Add all the fields from the response
Object.entries(fileData.fields).forEach(([key, value]) => {
    formData.append(key, value);
});

// Add the file last
formData.append('file', selectedFile);

// Upload directly to S3
const uploadResponse = await fetch(fileData.upload_url, {
    method: fileData.method,
    body: formData
});
```

### 3. Finalize Upload

**POST** `/fileman/finalize-upload/`

```javascript
const finalizeResponse = await fetch('/fileman/finalize-upload/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify({
        upload_token: fileData.upload_token,
        file_size: selectedFile.size,
        checksum: 'md5:' + calculatedMD5,
        metadata: {
            processing_complete: true
        }
    })
});

const result = await finalizeResponse.json();
```

**Response:**
```json
{
    "success": true,
    "file": {
        "id": 123,
        "filename": "document_20231201_abc12345.pdf",
        "original_filename": "document.pdf",
        "file_path": "uploads/document_20231201_abc12345.pdf",
        "file_size": 1024000,
        "content_type": "application/pdf",
        "upload_status": "completed",
        "download_url": "https://s3.amazonaws.com/my-bucket/...",
        "checksum": "md5:abcdef123456...",
        "metadata": {...},
        "created": "2023-12-01T12:00:00Z"
    }
}
```

## Storage Backend Configuration

### AWS S3

```python
{
    "name": "AWS S3 Production",
    "backend_type": "s3",
    "backend_url": "s3://my-bucket/",
    "supports_direct_upload": True,
    "settings": {
        "bucket_name": "my-bucket",
        "region_name": "us-east-1",
        "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "server_side_encryption": "AES256",
        "upload_expires_in": 3600,
        "download_expires_in": 3600
    }
}
```

### Local File System

```python
{
    "name": "Local Storage",
    "backend_type": "file",
    "backend_url": "file:///app/media/uploads/",
    "supports_direct_upload": False,
    "settings": {
        "base_path": "/app/media/uploads",
        "base_url": "/media/uploads/",
        "create_directories": True,
        "permissions": 0o644,
        "directory_permissions": 0o755
    }
}
```

### S3-Compatible Services (MinIO, DigitalOcean Spaces)

```python
{
    "name": "DigitalOcean Spaces",
    "backend_type": "s3",
    "backend_url": "https://nyc3.digitaloceanspaces.com/my-space/",
    "supports_direct_upload": True,
    "settings": {
        "bucket_name": "my-space",
        "region_name": "nyc3",
        "endpoint_url": "https://nyc3.digitaloceanspaces.com",
        "access_key_id": "DO00EXAMPLE",
        "secret_access_key": "EXAMPLE_SECRET_KEY"
    }
}
```

## JavaScript Upload Helper

```javascript
class FileUploader {
    constructor(csrfToken) {
        this.csrfToken = csrfToken;
    }

    async uploadFiles(files, options = {}) {
        // 1. Initiate upload
        const initResponse = await this.initiateUpload(files, options);
        if (!initResponse.success) {
            throw new Error('Failed to initiate upload');
        }

        // 2. Upload each file
        const uploadPromises = initResponse.files.map(async (fileData, index) => {
            const file = files[index];
            await this.uploadFile(file, fileData);
            return this.finalizeUpload(fileData.upload_token, file);
        });

        // 3. Wait for all uploads to complete
        return Promise.all(uploadPromises);
    }

    async initiateUpload(files, options) {
        const response = await fetch('/fileman/initiate-upload/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': this.csrfToken,
            },
            body: JSON.stringify({
                files: files.map(file => ({
                    filename: file.name,
                    content_type: file.type,
                    size: file.size
                })),
                ...options
            })
        });
        return response.json();
    }

    async uploadFile(file, uploadData) {
        const formData = new FormData();

        // Add fields for S3 or other backends
        Object.entries(uploadData.fields || {}).forEach(([key, value]) => {
            formData.append(key, value);
        });

        formData.append('file', file);

        const response = await fetch(uploadData.upload_url, {
            method: uploadData.method,
            body: formData
        });

        if (!response.ok) {
            throw new Error(`Upload failed: ${response.statusText}`);
        }
    }

    async finalizeUpload(uploadToken, file) {
        const response = await fetch('/fileman/finalize-upload/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': this.csrfToken,
            },
            body: JSON.stringify({
                upload_token: uploadToken,
                file_size: file.size
            })
        });
        return response.json();
    }
}

// Usage
const uploader = new FileUploader(document.querySelector('[name=csrfmiddlewaretoken]').value);
const fileInput = document.getElementById('file-input');

fileInput.addEventListener('change', async (e) => {
    const files = Array.from(e.target.files);
    try {
        const results = await uploader.uploadFiles(files, {
            metadata: { source: 'web_form' }
        });
        console.log('Upload completed:', results);
    } catch (error) {
        console.error('Upload failed:', error);
    }
});
```

## File Access and Downloads

### Download Files

```python
from mojo.apps.fileman.models import File
from mojo.apps.fileman.backends import get_backend

# Get file by upload token
file_obj = File.objects.get(upload_token='abc123...', upload_status=File.COMPLETED)

# Generate download URL
backend = get_backend(file_obj.file_manager)
download_url = backend.get_url(file_obj.file_path, expires_in=3600)

# Or use the built-in download view
# /fileman/download/abc123.../
```

### Check File Access Permissions

```python
# Check if user can access file
can_access = file_obj.can_be_accessed_by(user=request.user, group=user_group)

# Public files
file_obj.is_public = True
file_obj.save()
```

## Management Commands

### Clean Up Expired Uploads

```bash
# Clean up uploads older than 1 day (default)
python manage.py cleanup_expired_uploads

# Clean up uploads older than 6 hours
python manage.py cleanup_expired_uploads --hours 6

# Dry run to see what would be cleaned
python manage.py cleanup_expired_uploads --dry-run

# Clean up only failed uploads
python manage.py cleanup_expired_uploads --status failed

# Verbose output
python manage.py cleanup_expired_uploads --verbose
```

## File Validation

### Configure File Restrictions

```python
file_manager = FileManager.objects.create(
    name="Image Storage",
    backend_type="s3",
    max_file_size=10 * 1024 * 1024,  # 10MB
    allowed_extensions=["jpg", "jpeg", "png", "gif", "webp"],
    allowed_mime_types=[
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp"
    ],
    # ... other settings
)
```

### Custom Validation

```python
from mojo.apps.fileman.models import File

# Custom validation in your views
def validate_upload(request, file_data):
    # Custom business logic
    if file_data['filename'].startswith('temp_'):
        raise ValidationError('Temporary files not allowed')

    # Check file size against user's quota
    user_files_size = File.objects.filter(
        uploaded_by=request.user,
        upload_status=File.COMPLETED
    ).aggregate(total=Sum('file_size'))['total'] or 0

    if user_files_size + file_data['size'] > USER_QUOTA:
        raise ValidationError('Upload would exceed user quota')
```

## Security Considerations

### Environment Variables

```bash
# Production environment variables
export AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE"
export AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
export AWS_DEFAULT_REGION="us-east-1"
export KMS_KEY_ID="arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
```

### IAM Policy for S3

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject"
            ],
            "Resource": "arn:aws:s3:::my-bucket/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket"
            ],
            "Resource": "arn:aws:s3:::my-bucket"
        }
    ]
}
```

### CORS Configuration for S3

```json
[
    {
        "AllowedHeaders": ["*"],
        "AllowedMethods": ["GET", "POST", "PUT"],
        "AllowedOrigins": ["https://yourdomain.com"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000
    }
]
```

## Troubleshooting

### Common Issues

1. **Upload URL Expired**
   - Check `upload_expires_in` setting
   - Ensure client uploads immediately after getting URL

2. **CORS Errors on S3**
   - Configure CORS policy on S3 bucket
   - Ensure your domain is in AllowedOrigins

3. **File Not Found After Upload**
   - Check that finalize-upload was called successfully
   - Verify file exists in storage backend

4. **Permission Denied Errors**
   - Check IAM policies for S3
   - Verify file system permissions for local storage

5. **Large File Upload Failures**
   - Increase `max_file_size` setting
   - Consider enabling S3 multipart uploads for large files

### Debug Mode

```python
# Enable debug logging
import logging
logging.getLogger('mojo.apps.fileman').setLevel(logging.DEBUG)

# Check file status
file_obj = File.objects.get(upload_token='abc123...')
print(f"Status: {file_obj.upload_status}")
print(f"Path: {file_obj.file_path}")
print(f"Metadata: {file_obj.metadata}")

# Test backend connection
from mojo.apps.fileman.backends import get_backend
backend = get_backend(file_obj.file_manager)
is_valid, errors = backend.validate_configuration()
print(f"Backend valid: {is_valid}, Errors: {errors}")
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
