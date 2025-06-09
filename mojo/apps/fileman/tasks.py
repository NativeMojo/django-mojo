import logging
from celery import shared_task
from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer import process_new_file, get_renderer_for_file

logger = logging.getLogger(__name__)

@shared_task
def process_file_renditions(file_id):
    """
    Process renditions for a newly uploaded file
    
    Args:
        file_id: ID of the File to process
    
    Returns:
        dict: Result information including number of renditions created
    """
    try:
        file = File.objects.get(id=file_id)
        
        if not file.is_completed:
            logger.warning(f"Cannot process renditions for incomplete file {file_id}")
            return {
                'status': 'error',
                'message': 'File upload is not complete',
                'file_id': file_id,
                'renditions_created': 0
            }
        
        renditions = process_new_file(file)
        
        return {
            'status': 'success',
            'message': f'Created {len(renditions)} renditions',
            'file_id': file_id,
            'renditions_created': len(renditions),
            'rendition_roles': [r.role for r in renditions]
        }
        
    except File.DoesNotExist:
        logger.error(f"File with ID {file_id} not found")
        return {
            'status': 'error',
            'message': 'File not found',
            'file_id': file_id,
            'renditions_created': 0
        }
    except Exception as e:
        logger.exception(f"Error processing renditions for file {file_id}: {str(e)}")
        return {
            'status': 'error',
            'message': str(e),
            'file_id': file_id,
            'renditions_created': 0
        }

@shared_task
def cleanup_renditions(file_id):
    """
    Clean up all renditions for a file
    
    Args:
        file_id: ID of the File whose renditions should be cleaned up
    
    Returns:
        dict: Result information
    """
    try:
        # First get the file to make sure it exists
        file = File.objects.get(id=file_id)
        
        # Get all renditions
        renditions = FileRendition.objects.filter(original_file_id=file_id)
        count = renditions.count()
        
        # Delete the files from storage
        for rendition in renditions:
            try:
                if rendition.storage_path:
                    file.file_manager.backend.delete(rendition.storage_path)
            except Exception as e:
                logger.warning(f"Error deleting rendition file {rendition.id}: {str(e)}")
        
        # Delete the database records
        renditions.delete()
        
        return {
            'status': 'success',
            'message': f'Cleaned up {count} renditions',
            'file_id': file_id,
            'renditions_deleted': count
        }
        
    except File.DoesNotExist:
        logger.error(f"File with ID {file_id} not found")
        return {
            'status': 'error',
            'message': 'File not found',
            'file_id': file_id,
            'renditions_deleted': 0
        }
    except Exception as e:
        logger.exception(f"Error cleaning up renditions for file {file_id}: {str(e)}")
        return {
            'status': 'error',
            'message': str(e),
            'file_id': file_id,
            'renditions_deleted': 0
        }

@shared_task
def regenerate_renditions(file_id, roles=None):
    """
    Regenerate specific or all renditions for a file
    
    Args:
        file_id: ID of the File to process
        roles: Optional list of roles to regenerate (None for all)
    
    Returns:
        dict: Result information
    """
    try:
        file = File.objects.get(id=file_id)
        
        if not file.is_completed:
            logger.warning(f"Cannot regenerate renditions for incomplete file {file_id}")
            return {
                'status': 'error',
                'message': 'File upload is not complete',
                'file_id': file_id,
                'renditions_created': 0
            }
        
        # Get renderer for this file
        renderer = get_renderer_for_file(file)
        if not renderer:
            return {
                'status': 'error',
                'message': f'No renderer available for file type {file.category}',
                'file_id': file_id,
                'renditions_created': 0
            }
        
        # If specific roles are requested, delete and regenerate those
        if roles:
            # Delete existing renditions for these roles
            FileRendition.objects.filter(original_file=file, role__in=roles).delete()
            
            # Create new renditions
            created_renditions = []
            for role in roles:
                rendition = renderer.create_rendition(role)
                if rendition:
                    created_renditions.append(rendition)
        else:
            # Delete all existing renditions
            FileRendition.objects.filter(original_file=file).delete()
            
            # Create all default renditions
            created_renditions = renderer.create_all_renditions()
        
        return {
            'status': 'success',
            'message': f'Regenerated {len(created_renditions)} renditions',
            'file_id': file_id,
            'renditions_created': len(created_renditions),
            'rendition_roles': [r.role for r in created_renditions]
        }
        
    except File.DoesNotExist:
        logger.error(f"File with ID {file_id} not found")
        return {
            'status': 'error',
            'message': 'File not found',
            'file_id': file_id,
            'renditions_created': 0
        }
    except Exception as e:
        logger.exception(f"Error regenerating renditions for file {file_id}: {str(e)}")
        return {
            'status': 'error',
            'message': str(e),
            'file_id': file_id,
            'renditions_created': 0
        }

@shared_task
def process_bulk_renditions(file_ids, roles=None):
    """
    Process renditions for multiple files
    
    Args:
        file_ids: List of File IDs to process
        roles: Optional list of roles to generate (None for all default roles)
    
    Returns:
        dict: Result information
    """
    results = {
        'total': len(file_ids),
        'successful': 0,
        'failed': 0,
        'errors': []
    }
    
    for file_id in file_ids:
        try:
            file = File.objects.get(id=file_id)
            
            if not file.is_completed:
                results['failed'] += 1
                results['errors'].append({
                    'file_id': file_id,
                    'error': 'File upload is not complete'
                })
                continue
            
            renderer = get_renderer_for_file(file)
            if not renderer:
                results['failed'] += 1
                results['errors'].append({
                    'file_id': file_id,
                    'error': f'No renderer for file type {file.category}'
                })
                continue
            
            if roles:
                renditions = []
                for role in roles:
                    rendition = renderer.get_rendition(role)
                    if rendition:
                        renditions.append(rendition)
            else:
                renditions = renderer.create_all_renditions()
            
            results['successful'] += 1
            
        except File.DoesNotExist:
            results['failed'] += 1
            results['errors'].append({
                'file_id': file_id,
                'error': 'File not found'
            })
        except Exception as e:
            results['failed'] += 1
            results['errors'].append({
                'file_id': file_id,
                'error': str(e)
            })
            logger.exception(f"Error processing renditions for file {file_id}: {str(e)}")
    
    return results