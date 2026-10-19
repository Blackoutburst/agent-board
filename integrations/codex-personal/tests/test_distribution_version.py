"""The real distribution files must announce one version; no provider runs."""
import ast
from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from blackoutburst_agents import __version__
from blackoutburst_agents import cli


ADAPTER = Path(__file__).resolve().parents[1]
REPOSITORY = ADAPTER.parents[1]


def declared_versions():
    tree = ast.parse((ADAPTER/'blackoutburst_agents/__init__.py').read_bytes())
    initializers = [ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
        and any(isinstance(target,ast.Name) and target.id=='__version__' for target in node.targets)]
    project = re.findall(r'(?ms)^\[project\][ \t]*\n(.*?)(?=^\[|\Z)',(ADAPTER/'pyproject.toml').read_text())
    if len(initializers)!=1 or len(project)!=1:
        raise AssertionError('Expected one static project and runtime version')
    metadata = re.findall(r'(?m)^version[ \t]*=[ \t]*("[^"]+")[ \t]*$',project[0])
    if len(metadata)!=1:
        raise AssertionError('Expected one static project metadata version')
    return initializers[0],ast.literal_eval(metadata[0])


class DistributionVersionTests(unittest.TestCase):
    def test_real_build_metadata_matches_the_runtime_initializer(self):
        runtime,metadata=declared_versions()
        self.assertRegex(runtime,r'^0\.[0-9]+\.[0-9]+$')
        self.assertEqual(metadata,runtime,'Build metadata and runtime would describe different releases')
        self.assertEqual(__version__,runtime,'Imported CLI must use this source distribution')

    def test_version_only_cli_is_the_same_version_without_any_provider_access(self):
        stream=io.StringIO()
        with patch('subprocess.Popen',side_effect=AssertionError('No process for version metadata')), \
             patch('socket.socket',side_effect=AssertionError('No network for version metadata')), \
             patch.object(cli,'inspect_cli',side_effect=AssertionError('No login-status for version metadata')), \
             patch.object(cli,'run_subscription_probe',side_effect=AssertionError('No model for version metadata')), \
             redirect_stdout(stream),self.assertRaises(SystemExit) as ended:
            cli.main(['--version'])
        self.assertEqual(ended.exception.code,0)
        self.assertEqual(stream.getvalue(),__version__+'\n')

    def test_current_distribution_announcements_match_without_relabeling_history(self):
        version,_=declared_versions()
        integration=(ADAPTER/'INTEGRATION.md').read_text().splitlines()[2]
        operations=(REPOSITORY/'docs/OPERATIONS.md').read_text().splitlines()[2]
        self.assertIn('adaptateur **'+version+'**',integration)
        self.assertIn('adapter '+version+'.',operations)


if __name__=='__main__':unittest.main()
