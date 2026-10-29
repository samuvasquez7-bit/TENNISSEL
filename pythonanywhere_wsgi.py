import os
import sys


project_root = os.path.abspath(os.path.dirname(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

os.environ.setdefault(
    'DATABASE_PATH',
    os.path.join(project_root, 'instance', 'tennissel.sqlite3'),
)

from app import app as application
