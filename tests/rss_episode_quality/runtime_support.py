import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT=Path(__file__).resolve().parents[2]/'plugins.v3'/'rssepisodequality'
pkg=ModuleType('rss_under_test'); pkg.__path__=[str(ROOT)]
sys.modules.setdefault('rss_under_test',pkg)

def load(name):
    fullname='rss_under_test.'+name
    if fullname in sys.modules: return sys.modules[fullname]
    spec=importlib.util.spec_from_file_location(fullname,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec); sys.modules[fullname]=module
    spec.loader.exec_module(module)
    return module
