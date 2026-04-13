import mojo.decorators as md
from mojo.apps.github.models import GitHubInstall


@md.URL("github_install")
@md.URL("github_install/<int:pk>")
@md.uses_model_security(GitHubInstall)
def on_github_install(request, pk=None):
    return GitHubInstall.on_rest_request(request, pk)
