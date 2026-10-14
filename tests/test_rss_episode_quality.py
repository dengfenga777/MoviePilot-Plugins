"""Entry point for the current RSS policy regression suite.

1.1.0 replaces permanent history-based suppression and whole-pack downloads;
its expectations live with the new media-library and strict-selection suite.
"""
from pathlib import Path
import unittest


def load_tests(loader, tests, pattern):
    root = Path(__file__).parent / 'rss_episode_quality'
    return loader.discover(str(root), pattern='test_*.py', top_level_dir=str(root))


if __name__ == '__main__':
    unittest.main()
