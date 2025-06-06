"""
Complete usage example for Django File Manager (fileman)

This example demonstrates how to use the fileman system for file uploads
with both S3 and local file system backends.
"""

import os
import json
import hashlib
from datetime import datetime, timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from mojo.apps.fileman.models import FileManager, File
from mojo.apps.fileman.backends import get_backend
from mojo.apps.account.models import Group

User = get_user_model()


class FilemanUsageExample:
    """
    Complete example of using the fileman system
    """
    
    def __init__(self):
        self.client = Client()
        self.user = None
        self.group = None
        self.s3_file_manager = None
        self.local_file_manager = None
    
    def setup_test_data(self):
        """Set up test users, groups, and file managers"""
        
        # Create test user and group
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.group = Group.objects.create(
            name='Test Group',
            description='Group for testing file uploads'
        )
        
        # Add user to group (assuming GroupMember model exists)
        # GroupMember.objects.create(user=self.user, group=self.group)
        
        # Create S3 file manager
        self.s3_file_manager = FileManager.objects.create(
            name="AWS S3 Test Storage",
            description="S3 storage for testing",
            backend_type="s3",
            backend_url="s3://test-bucket/",
            supports_direct_upload=True,
            max_file_size=100 * 1024 * 1024,  # 100MB
            allowed_extensions=["pdf", "jpg", "png", "txt"],
            allowed_mime_types=[
                "application/pdf",
                "image/jpeg", 
                "image/png",
                "text/plain"
            ],
            settings={
                "bucket_name": "test-bucket",
                "region_name": "us-east-1",
                "access_key_id": "test_key_id",
                "secret_access_key": "test_secret_key",
                "upload_expires_in": 3600,
                "download_expires_in": 3600,
                "server_side_encryption": "AES256"
            },
            group=self.group,
            is_default=True,
            is_active=True
        )
        
        # Create local file system manager
        self.local_file_manager = FileManager.objects.create(
            name="Local File Storage",
            description="Local file system for testing",
            backend_type="file", 
            backend_url="file:///tmp/test_uploads/",
            supports_direct_upload=False,
            max_file_size=50 * 1024 * 1024,  # 50MB
            settings={
                "base_path": "/tmp/test_uploads",
                "base_url": "/media/test/",
                "create_directories": True,
                "permissions": 0o644,
                "directory_permissions": 0o755,
                "temp_upload_path": "/tmp/test_uploads/temp"
            },
            group=self.group,
            is_active=True
        )
    
    def example_1_initiate_single_upload(self):
        """Example 1: Initiate upload for a single file"""
        
        print("=== Example 1: Initiate Single File Upload ===")
        
        # Login user
        self.client.login(username='testuser', password='testpass123')
        
        # Prepare upload request
        upload_data = {
            "files": [
                {
                    "filename": "test_document.pdf",
                    "content_type": "application/pdf",
                    "size": 1024000  # 1MB
                }
            ],
            "file_manager_id": self.s3_file_manager.id,
            "metadata": {
                "source": "example_upload",
                "category": "documents",
                "description": "Test document upload"
            }
        }
        
        # Initiate upload
        response = self.client.post(
            '/fileman/initiate-upload/',
            data=json.dumps(upload_data),
            content_type='application/json'
        )
        
        print(f"Response status: {response.status_code}")
        result = response.json()
        print(f"Success: {result.get('success')}")
        
        if result.get('success'):
            file_data = result['files'][0]
            print(f"Upload token: {file_data['upload_token']}")
            print(f"Upload URL: {file_data['upload_url']}")
            print(f"Method: {file_data['method']}")
            print(f"Fields: {file_data.get('fields', {})}")
            
            return file_data
        else:
            print(f"Error: {result.get('error')}")
            return None
    
    def example_2_initiate_multiple_uploads(self):
        """Example 2: Initiate upload for multiple files"""
        
        print("\n=== Example 2: Initiate Multiple File Upload ===")
        
        # Prepare multiple files
        upload_data = {
            "files": [
                {
                    "filename": "image1.jpg",
                    "content_type": "image/jpeg", 
                    "size": 512000
                },
                {
                    "filename": "image2.png",
                    "content_type": "image/png",
                    "size": 768000
                },
                {
                    "filename": "readme.txt",
                    "content_type": "text/plain",
                    "size": 4096
                }
            ],
            "metadata": {
                "batch_id": "batch_001",
                "uploaded_via": "web_interface"
            }
        }
        
        response = self.client.post(
            '/fileman/initiate-upload/',
            data=json.dumps(upload_data),
            content_type='application/json'
        )
        
        result = response.json()
        print(f"Multiple upload success: {result.get('success')}")
        print(f"Number of files initiated: {len(result.get('files', []))}")
        
        if result.get('errors'):
            print(f"Errors: {result['errors']}")
        
        return result.get('files', [])
    
    def example_3_direct_upload_simulation(self, file_data):
        """Example 3: Simulate direct upload to S3"""
        
        print("\n=== Example 3: Simulate Direct Upload ===")
        
        if not file_data:
            print("No file data available for upload")
            return False
        
        print(f"Simulating upload to: {file_data['upload_url']}")
        print(f"Using method: {file_data['method']}")
        
        # In a real scenario, this would be done by the client (browser)
        # uploading directly to S3 using the pre-signed URL
        
        # For demonstration, we'll just print what would happen
        print("Client would now:")
        print("1. Create FormData with the provided fields")
        print("2. Add the actual file to the FormData")
        print("3. POST to the upload_url")
        print("4. Handle the response from S3")
        
        # Simulate successful upload
        print("✓ Simulated successful upload to S3")
        return True
    
    def example_4_finalize_upload(self, file_data):
        """Example 4: Finalize the upload"""
        
        print("\n=== Example 4: Finalize Upload ===")
        
        if not file_data:
            print("No file data available for finalization")
            return None
        
        # Create test file content for checksum
        test_content = b"This is test PDF content"
        md5_hash = hashlib.md5(test_content).hexdigest()
        
        finalize_data = {
            "upload_token": file_data['upload_token'],
            "file_size": len(test_content),
            "checksum": f"md5:{md5_hash}",
            "metadata": {
                "finalized_at": datetime.now().isoformat(),
                "processing_complete": True
            }
        }
        
        response = self.client.post(
            '/fileman/finalize-upload/',
            data=json.dumps(finalize_data),
            content_type='application/json'
        )
        
        result = response.json()
        print(f"Finalization success: {result.get('success')}")
        
        if result.get('success'):
            file_info = result['file']
            print(f"File ID: {file_info['id']}")
            print(f"Final filename: {file_info['filename']}")
            print(f"File path: {file_info['file_path']}")
            print(f"Status: {file_info['upload_status']}")
            print(f"Download URL: {file_info['download_url']}")
            
            return file_info
        else:
            print(f"Finalization error: {result.get('error')}")
            return None
    
    def example_5_local_file_upload(self):
        """Example 5: Upload to local file system backend"""
        
        print("\n=== Example 5: Local File System Upload ===")
        
        # Create a test file
        test_content = b"This is a test file for local upload"
        test_file = SimpleUploadedFile(
            "test_local.txt",
            test_content,
            content_type="text/plain"
        )
        
        # First initiate upload for local backend
        upload_data = {
            "files": [
                {
                    "filename": "test_local.txt",
                    "content_type": "text/plain",
                    "size": len(test_content)
                }
            ],
            "file_manager_id": self.local_file_manager.id
        }
        
        response = self.client.post(
            '/fileman/initiate-upload/',
            data=json.dumps(upload_data),
            content_type='application/json'
        )
        
        result = response.json()
        if not result.get('success'):
            print(f"Failed to initiate local upload: {result.get('error')}")
            return None
        
        file_data = result['files'][0]
        upload_token = file_data['upload_token']
        
        print(f"Local upload initiated with token: {upload_token}")
        
        # Upload file using the direct upload endpoint
        response = self.client.post(
            f'/fileman/upload/{upload_token}/',
            data={'file': test_file},
            format='multipart'
        )
        
        result = response.json()
        print(f"Direct upload success: {result.get('success')}")
        
        if result.get('success'):
            # Finalize the upload
            finalize_data = {
                "upload_token": upload_token,
                "file_size": len(test_content)
            }
            
            response = self.client.post(
                '/fileman/finalize-upload/',
                data=json.dumps(finalize_data),
                content_type='application/json'
            )
            
            result = response.json()
            print(f"Local upload finalized: {result.get('success')}")
            return result.get('file')
        
        return None
    
    def example_6_file_management(self):
        """Example 6: File management operations"""
        
        print("\n=== Example 6: File Management ===")
        
        # Get all files uploaded by user
        user_files = File.objects.filter(
            uploaded_by=self.user,
            upload_status=File.COMPLETED
        )
        
        print(f"User has {user_files.count()} completed uploads")
        
        for file_obj in user_files:
            print(f"- {file_obj.original_filename} ({file_obj.get_human_readable_size()})")
            print(f"  Status: {file_obj.get_upload_status_display()}")
            print(f"  Path: {file_obj.file_path}")
            print(f"  Created: {file_obj.created}")
            
            # Check if file exists in backend
            try:
                backend = get_backend(file_obj.file_manager)
                exists = backend.exists(file_obj.file_path)
                print(f"  Exists in storage: {exists}")
                
                if exists:
                    file_size = backend.get_file_size(file_obj.file_path)
                    print(f"  Backend file size: {file_size}")
                    
                    # Generate download URL
                    download_url = backend.get_url(file_obj.file_path, expires_in=300)
                    print(f"  Download URL: {download_url}")
                    
            except Exception as e:
                print(f"  Backend error: {e}")
            
            print()
    
    def example_7_download_file(self, upload_token):
        """Example 7: Download a file"""
        
        print("\n=== Example 7: Download File ===")
        
        if not upload_token:
            print("No upload token provided")
            return
        
        # Use the download endpoint
        response = self.client.get(f'/fileman/download/{upload_token}/')
        
        print(f"Download response status: {response.status_code}")
        
        if response.status_code == 302:  # Redirect to actual file
            print(f"Redirected to: {response['Location']}")
        elif response.status_code == 200:
            print("File downloaded successfully")
            print(f"Content type: {response.get('Content-Type')}")
            print(f"Content length: {response.get('Content-Length')}")
        else:
            try:
                result = response.json()
                print(f"Download error: {result.get('error')}")
            except:
                print(f"Download failed with status {response.status_code}")
    
    def example_8_backend_operations(self):
        """Example 8: Direct backend operations"""
        
        print("\n=== Example 8: Backend Operations ===")
        
        # Test S3 backend
        try:
            s3_backend = get_backend(self.s3_file_manager)
            print(f"S3 Backend: {s3_backend}")
            
            # Test configuration validation
            is_valid, errors = s3_backend.validate_configuration()
            print(f"S3 config valid: {is_valid}")
            if errors:
                print(f"S3 errors: {errors}")
            
            # Test file operations (if backend is properly configured)
            test_path = "test/example.txt"
            test_content = b"Hello from S3 backend test"
            
            print(f"Testing file operations with path: {test_path}")
            
        except Exception as e:
            print(f"S3 backend error: {e}")
        
        # Test local backend
        try:
            local_backend = get_backend(self.local_file_manager)
            print(f"Local Backend: {local_backend}")
            
            # Test configuration validation
            is_valid, errors = local_backend.validate_configuration()
            print(f"Local config valid: {is_valid}")
            if errors:
                print(f"Local errors: {errors}")
            
            # Test file operations
            import io
            test_content = b"Hello from local backend test"
            test_file = io.BytesIO(test_content)
            
            saved_path = local_backend.save(test_file, "backend_test.txt")
            print(f"Saved test file to: {saved_path}")
            
            # Check if file exists
            exists = local_backend.exists(saved_path)
            print(f"File exists: {exists}")
            
            if exists:
                file_size = local_backend.get_file_size(saved_path)
                print(f"File size: {file_size}")
                
                # Generate URL
                file_url = local_backend.get_url(saved_path)
                print(f"File URL: {file_url}")
                
                # Clean up
                deleted = local_backend.delete(saved_path)
                print(f"File deleted: {deleted}")
            
        except Exception as e:
            print(f"Local backend error: {e}")
    
    def example_9_cleanup_operations(self):
        """Example 9: Cleanup operations"""
        
        print("\n=== Example 9: Cleanup Operations ===")
        
        # Find expired uploads
        from django.utils import timezone
        cutoff_date = timezone.now() - timedelta(hours=1)
        
        expired_files = File.objects.filter(
            upload_expires_at__lt=timezone.now(),
            upload_status__in=[File.PENDING, File.UPLOADING]
        )
        
        print(f"Found {expired_files.count()} expired uploads")
        
        for file_obj in expired_files:
            print(f"- {file_obj.original_filename} (expired: {file_obj.upload_expires_at})")
            file_obj.mark_as_expired()
        
        # Find failed uploads older than cutoff
        old_failed = File.objects.filter(
            created__lt=cutoff_date,
            upload_status=File.FAILED
        )
        
        print(f"Found {old_failed.count()} old failed uploads")
        
        # Backend cleanup
        for file_manager in [self.s3_file_manager, self.local_file_manager]:
            try:
                backend = get_backend(file_manager)
                print(f"Running cleanup for {file_manager.name}")
                backend.cleanup_expired_uploads(cutoff_date)
            except Exception as e:
                print(f"Cleanup error for {file_manager.name}: {e}")
    
    def run_all_examples(self):
        """Run all examples in sequence"""
        
        print("Django File Manager Usage Examples")
        print("==================================")
        
        # Setup
        self.setup_test_data()
        print("✓ Test data setup complete")
        
        # Run examples
        file_data = self.example_1_initiate_single_upload()
        self.example_2_initiate_multiple_uploads()
        
        if file_data:
            upload_success = self.example_3_direct_upload_simulation(file_data)
            if upload_success:
                finalized_file = self.example_4_finalize_upload(file_data)
                if finalized_file:
                    self.example_7_download_file(file_data['upload_token'])
        
        local_file = self.example_5_local_file_upload()
        self.example_6_file_management()
        self.example_8_backend_operations()
        self.example_9_cleanup_operations()
        
        print("\n=== All Examples Complete ===")


# JavaScript client example for browser usage
JAVASCRIPT_EXAMPLE = """
// JavaScript example for browser-based file uploads

class FilemanClient {
    constructor(csrfToken) {
        this.csrfToken = csrfToken;
        this.baseUrl = '/fileman';
    }

    async uploadFiles(files, options = {}) {
        try {
            // 1. Initiate upload
            const initResponse = await this.initiateUpload(files, options);
            if (!initResponse.success) {
                throw new Error(`Failed to initiate upload: ${initResponse.error}`);
            }

            // 2. Upload each file
            const uploadPromises = initResponse.files.map(async (fileData, index) => {
                const file = files[index];
                
                // Upload to pre-signed URL or direct endpoint
                await this.uploadFile(file, fileData);
                
                // Finalize upload
                return this.finalizeUpload(fileData.upload_token, file);
            });

            // 3. Wait for all uploads
            const results = await Promise.all(uploadPromises);
            return results;
            
        } catch (error) {
            console.error('Upload failed:', error);
            throw error;
        }
    }

    async initiateUpload(files, options) {
        const response = await fetch(`${this.baseUrl}/initiate-upload/`, {
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
        
        // Add file last
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
        const response = await fetch(`${this.baseUrl}/finalize-upload/`, {
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

// Usage example:
const fileInput = document.getElementById('file-input');
const uploader = new FilemanClient(getCsrfToken());

fileInput.addEventListener('change', async (e) => {
    const files = Array.from(e.target.files);
    
    try {
        const results = await uploader.uploadFiles(files, {
            metadata: { source: 'web_upload' }
        });
        
        console.log('Upload completed:', results);
        
        // Handle successful uploads
        results.forEach(result => {
            if (result.success) {
                console.log(`Uploaded: ${result.file.original_filename}`);
                // Update UI, show download links, etc.
            }
        });
        
    } catch (error) {
        console.error('Upload error:', error);
        // Handle error - show message to user
    }
});
"""


if __name__ == "__main__":
    # This would typically be run in a Django environment
    print("This example should be run within a Django environment")
    print("You can copy the FilemanUsageExample class and adapt it for your needs")
    
    # Uncomment to run examples (requires Django setup):
    # example = FilemanUsageExample()
    # example.run_all_examples()