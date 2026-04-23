from mojo import decorators as md
from mojo.apps.fileman.models import File, FileManager


@md.URL('manager')
@md.URL('manager/<int:pk>')
@md.uses_model_security(FileManager)
def on_filemanager(request, pk=None):
    return FileManager.on_rest_request(request, pk)

@md.URL('file')
@md.URL('file/<int:pk>')
@md.uses_model_security(File)
def on_file(request, pk=None):
    return File.on_rest_request(request, pk)
