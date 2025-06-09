import os
import io
import subprocess
import logging
import mimetypes
import tempfile
from typing import Dict, Optional, Tuple, Union, BinaryIO, List
import shutil

from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.base import BaseRenderer, RenditionRole

logger = logging.getLogger(__name__)

class DocumentRenderer(BaseRenderer):
    """
    Renderer for document files
    
    Creates various renditions like thumbnails and previews for document files
    such as PDFs, Word documents, Excel spreadsheets, etc.
    """
    
    # Document file categories
    supported_categories = ['document', 'pdf', 'spreadsheet', 'presentation']
    
    # Default rendition definitions with options
    default_renditions = {
        RenditionRole.DOCUMENT_THUMBNAIL: {
            'width': 300,
            'height': 424,  # Roughly A4 proportions
            'format': 'jpg',
            'page': 1
        },
        RenditionRole.THUMBNAIL: {
            'width': 200,
            'height': 283,  # Roughly A4 proportions
            'format': 'jpg',
            'page': 1
        },
        RenditionRole.DOCUMENT_PREVIEW: {
            'format': 'pdf',
            'quality': 'medium',
            'max_pages': 20,  # Limit preview to first 20 pages
        },
        RenditionRole.DOCUMENT_PDF: {
            'format': 'pdf',
            'quality': 'high',
        },
    }
    
    # Document format conversions supported
    conversion_map = {
        # Office formats
        '.doc': 'pdf',
        '.docx': 'pdf',
        '.xls': 'pdf',
        '.xlsx': 'pdf',
        '.ppt': 'pdf',
        '.pptx': 'pdf',
        '.odt': 'pdf',
        '.ods': 'pdf',
        '.odp': 'pdf',
        # Text formats
        '.txt': 'pdf',
        '.rtf': 'pdf',
        '.md': 'pdf',
        # Other formats
        '.epub': 'pdf',
    }
    
    def __init__(self, file: File):
        super().__init__(file)
        # Check if required tools are available
        self._check_dependencies()
    
    def _check_dependencies(self):
        """Check if required tools are available in the system"""
        # Check for pdftoppm (for PDF thumbnails)
        try:
            subprocess.run(["pdftoppm", "-v"], 
                           stdout=subprocess.PIPE, 
                           stderr=subprocess.PIPE, 
                           check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.warning("pdftoppm is not available. PDF thumbnail generation may not work properly.")
        
        # Check for LibreOffice (for document conversion)
        try:
            subprocess.run(["libreoffice", "--version"], 
                           stdout=subprocess.PIPE, 
                           stderr=subprocess.PIPE, 
                           check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.warning("LibreOffice is not available. Document conversion may not work properly.")
    
    def _download_original(self) -> Union[str, None]:
        """
        Download the original file to a temporary location
        
        Returns:
            str: Path to the downloaded file, or None if download failed
        """
        try:
            file_manager = self.file.file_manager
            backend = file_manager.backend
            
            # Get file extension
            _, ext = os.path.splitext(self.file.filename)
            temp_path = self.get_temp_path(ext)
            
            # Download file from storage
            with open(temp_path, 'wb') as f:
                backend.download(self.file.storage_file_path, f)
            
            return temp_path
        except Exception as e:
            logger.error(f"Failed to download original document file: {str(e)}")
            return None
    
    def _convert_to_pdf(self, source_path: str) -> Tuple[str, bool]:
        """
        Convert document to PDF using LibreOffice
        
        Args:
            source_path: Path to the source document
            
        Returns:
            Tuple[str, bool]: (Output PDF path, success status)
        """
        _, ext = os.path.splitext(source_path.lower())
        
        # If already PDF, just return the path
        if ext == '.pdf':
            return source_path, True
        
        # Check if we support converting this format
        if ext not in self.conversion_map:
            logger.warning(f"Unsupported document format for conversion: {ext}")
            return None, False
        
        # Create a temporary directory for the conversion
        temp_dir = tempfile.mkdtemp()
        try:
            # Copy the source file to the temp directory
            temp_input = os.path.join(temp_dir, os.path.basename(source_path))
            shutil.copy2(source_path, temp_input)
            
            # Use LibreOffice to convert to PDF
            cmd = [
                "libreoffice",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", temp_dir,
                temp_input
            ]
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Find the output PDF
            base_name = os.path.splitext(os.path.basename(source_path))[0]
            output_pdf = os.path.join(temp_dir, f"{base_name}.pdf")
            
            if not os.path.exists(output_pdf):
                logger.error("PDF conversion failed - output file not found")
                return None, False
            
            # Copy to a location outside the temp dir
            final_pdf = self.get_temp_path(".pdf")
            shutil.copy2(output_pdf, final_pdf)
            
            return final_pdf, True
            
        except subprocess.SubprocessError as e:
            logger.error(f"Document conversion failed: {str(e)}")
            return None, False
        finally:
            # Clean up temp directory
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    def _create_pdf_thumbnail(self, pdf_path: str, width: int, height: int, 
                            page: int, output_format: str) -> Tuple[str, str, int]:
        """
        Create a thumbnail from a PDF
        
        Args:
            pdf_path: Path to the PDF
            width: Target width
            height: Target height
            page: Page number to use (1-based)
            output_format: Output format (jpg, png)
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size)
        """
        temp_prefix = self.get_temp_path("")
        
        try:
            # Use pdftoppm to extract page as image
            cmd = [
                "pdftoppm",
                "-f", str(page),  # First page
                "-l", str(page),  # Last page (same as first)
                "-scale-to-x", str(width),
                "-scale-to-y", str(height),
                "-singlefile",  # Output a single file
            ]
            
            # Set format
            if output_format.lower() == 'jpg':
                cmd.append("-jpeg")
            elif output_format.lower() == 'png':
                cmd.append("-png")
            
            # Add input and output paths
            cmd.extend([pdf_path, temp_prefix])
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # pdftoppm adds the format as suffix
            output_file = f"{temp_prefix}.{output_format}"
            
            if not os.path.exists(output_file):
                logger.error("PDF thumbnail generation failed - output file not found")
                return None, None, 0
            
            # Get file size
            file_size = os.path.getsize(output_file)
            
            # Get mime type
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            
            return output_file, mime_type, file_size
            
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to create PDF thumbnail: {str(e)}")
            return None, None, 0
    
    def _optimize_pdf(self, pdf_path: str, quality: str = 'medium') -> Tuple[str, int]:
        """
        Optimize a PDF file to reduce size
        
        Args:
            pdf_path: Path to the PDF
            quality: Quality level ('low', 'medium', 'high')
            
        Returns:
            Tuple[str, int]: (Output path, file size)
        """
        output_path = self.get_temp_path(".pdf")
        
        try:
            # Set Ghostscript parameters based on quality
            if quality == 'low':
                params = ["-dPDFSETTINGS=/screen"]  # lowest quality, smallest size
            elif quality == 'medium':
                params = ["-dPDFSETTINGS=/ebook"]  # medium quality, medium size
            else:  # high
                params = ["-dPDFSETTINGS=/prepress"]  # high quality, larger size
            
            # Use Ghostscript to optimize
            cmd = [
                "gs",
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.4",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
            ] + params + [
                f"-sOutputFile={output_path}",
                pdf_path
            ]
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            if not os.path.exists(output_path):
                logger.error("PDF optimization failed - output file not found")
                return pdf_path, os.path.getsize(pdf_path)
            
            # Get file size
            file_size = os.path.getsize(output_path)
            
            return output_path, file_size
            
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to optimize PDF: {str(e)}")
            return pdf_path, os.path.getsize(pdf_path)
    
    def create_rendition(self, role: str, options: Dict = None) -> Optional[FileRendition]:
        """
        Create a document rendition for the specified role
        
        Args:
            role: The role of the rendition
            options: Additional options for creating the rendition
            
        Returns:
            FileRendition: The created rendition, or None if creation failed
        """
        try:
            # Get rendition settings
            settings = self.default_renditions.get(role, {})
            if options:
                settings.update(options)
            
            # Download the original file
            source_path = self._download_original()
            if not source_path:
                return None
            
            temp_files = [source_path]  # Track temporary files to clean up
            
            try:
                # First convert to PDF if needed
                if role in [RenditionRole.DOCUMENT_PDF, RenditionRole.DOCUMENT_PREVIEW]:
                    pdf_path, success = self._convert_to_pdf(source_path)
                    if not success:
                        return None
                    
                    temp_files.append(pdf_path)
                    
                    # For preview or PDF rendition
                    quality = settings.get('quality', 'medium')
                    
                    # Optimize the PDF
                    optimized_pdf, file_size = self._optimize_pdf(pdf_path, quality)
                    temp_files.append(optimized_pdf)
                    
                    # Set output details
                    temp_output = optimized_pdf
                    mime_type = "application/pdf"
                    
                    # Set filename
                    name, _ = os.path.splitext(self.file.filename)
                    filename = f"{name}_{role}.pdf"
                    category = 'document'
                    
                elif role in [RenditionRole.THUMBNAIL, RenditionRole.DOCUMENT_THUMBNAIL]:
                    # First make sure we have a PDF
                    pdf_path, success = self._convert_to_pdf(source_path)
                    if not success:
                        return None
                    
                    temp_files.append(pdf_path)
                    
                    # Create thumbnail image
                    width = settings.get('width', 200)
                    height = settings.get('height', 283)
                    page = settings.get('page', 1)
                    output_format = settings.get('format', 'jpg')
                    
                    temp_output, mime_type, file_size = self._create_pdf_thumbnail(
                        pdf_path, width, height, page, output_format
                    )
                    
                    if not temp_output:
                        return None
                    
                    temp_files.append(temp_output)
                    
                    # Set filename
                    name, _ = os.path.splitext(self.file.filename)
                    filename = f"{name}_{role}.{output_format}"
                    category = 'image'  # Thumbnails are images
                
                else:
                    logger.warning(f"Unsupported rendition role for documents: {role}")
                    return None
                
                # Save to storage
                file_manager = self.file.file_manager
                backend = file_manager.backend
                storage_path = os.path.join(
                    os.path.dirname(self.file.storage_file_path),
                    filename
                )
                
                # Upload to storage
                with open(temp_output, 'rb') as f:
                    backend.save(f, storage_path, mime_type)
                
                # Create rendition record
                rendition = self._create_rendition_object(
                    role=role,
                    filename=filename,
                    storage_path=storage_path,
                    content_type=mime_type,
                    category=category,
                    file_size=file_size
                )
                
                return rendition
                
            finally:
                # Clean up temporary files
                for temp_file in temp_files:
                    if temp_file and os.path.exists(temp_file):
                        try:
                            os.unlink(temp_file)
                        except:
                            pass
                    
        except Exception as e:
            logger.error(f"Failed to create document rendition '{role}': {str(e)}")
            return None