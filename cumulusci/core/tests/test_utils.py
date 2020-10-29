import datetime
import os
import unittest
from unittest import mock
from tempfile import TemporaryDirectory
from pathlib import Path

import pytz
import pytest

from .. import utils

from cumulusci.core.exceptions import ConfigMergeError
from cumulusci.utils import temporary_dir, touch
from cumulusci.core.utils import cleanup_org_cache_dirs
from cumulusci.core.config import OrgConfig


class TestUtils(unittest.TestCase):
    def test_parse_datetime(self):
        dt = utils.parse_datetime("2018-07-30", "%Y-%m-%d")
        self.assertEqual(dt, datetime.datetime(2018, 7, 30, 0, 0, 0, 0, pytz.UTC))

    def test_process_bool_arg(self):
        for arg in (True, "True", "true", "1"):
            self.assertTrue(utils.process_bool_arg(arg))

        for arg in (False, "False", "false", "0"):
            self.assertFalse(utils.process_bool_arg(arg))

        import warnings

        with warnings.catch_warnings(record=True):
            assert utils.process_bool_arg(None) is False

        with pytest.raises(TypeError):
            utils.process_bool_arg(datetime.datetime.now())

        with pytest.raises(TypeError):
            utils.process_bool_arg("xyzzy")

    def test_process_list_arg(self):
        self.assertEqual([1, 2], utils.process_list_arg([1, 2]))
        self.assertEqual(["a", "b"], utils.process_list_arg("a, b"))
        self.assertEqual(None, utils.process_list_arg(None))

    def test_process_glob_list_arg(self):
        with temporary_dir():
            touch("foo.py")
            touch("bar.robot")

            # Expect passing arg as list works.
            self.assertEqual(
                ["foo.py", "bar.robot"],
                utils.process_glob_list_arg(["foo.py", "bar.robot"]),
            )

            # Falsy arg should return an empty list
            self.assertEqual([], utils.process_glob_list_arg(None))
            self.assertEqual([], utils.process_glob_list_arg(""))
            self.assertEqual([], utils.process_glob_list_arg([]))

            # Expect output to be in order given
            self.assertEqual(
                ["foo.py", "bar.robot"],
                utils.process_glob_list_arg("foo.py, bar.robot"),
            )

            # Expect sorted output of glob results
            self.assertEqual(["bar.robot", "foo.py"], utils.process_glob_list_arg("*"))

            # Patterns that don't match any files
            self.assertEqual(
                ["*.bar", "x.y.z"], utils.process_glob_list_arg("*.bar, x.y.z")
            )

            # Recursive
            os.mkdir("subdir")
            filename = os.path.join("subdir", "baz.resource")
            touch(filename)
            self.assertEqual([filename], utils.process_glob_list_arg("**/*.resource"))

    def test_decode_to_unicode(self):
        self.assertEqual(u"\xfc", utils.decode_to_unicode(b"\xfc"))
        self.assertEqual(u"\u2603", utils.decode_to_unicode(u"\u2603"))
        self.assertEqual(None, utils.decode_to_unicode(None))


class TestMergedConfig(unittest.TestCase):
    def test_init(self):
        config = utils.merge_config(
            {
                "universal_config": {"hello": "world"},
                "user_config": {"hello": "christian"},
            }
        )
        self.assertEqual(config["hello"], "christian")

    def test_merge_failure(self):
        with self.assertRaises(ConfigMergeError) as cm:
            utils.merge_config(
                {
                    "universal_config": {"hello": "world", "test": {"sample": 1}},
                    "user_config": {"hello": "christian", "test": [1, 2]},
                }
            )
        exception = cm.exception
        self.assertEqual(exception.config_name, "user_config")


class TestDictMerger(unittest.TestCase):
    """ some stuff that didnt get covered by usual usage  """

    def test_merge_into_list(self):
        combo = utils.dictmerge([1, 2], 3)
        self.assertSequenceEqual(combo, [1, 2, 3])

    def test_cant_merge_into_dict(self):
        with self.assertRaises(ConfigMergeError):
            utils.dictmerge({"a": "b"}, 2)

    def test_cant_merge_nonsense(self):
        with self.assertRaises(ConfigMergeError):
            utils.dictmerge(pytz, 2)

    def test_merge_dict(self):
        d1 = {"s": "str", "l": [1, 2, 3], "d": {"another": "dict"}}
        d2 = {"i": "str", "l": [4], "d": {"another": "dict"}}
        merged = utils.dictmerge(d1, d2)
        assert merged == {
            "s": "str",
            "i": "str",
            "l": [1, 2, 3, 4],
            "d": {"another": "dict"},
        }

    def test_merge_dict__with_prefix(self):
        d1 = {"s": "str", "l": [1, 2, 3], "d": {"another": "dict"}}
        d2 = {"i": "str", "l": [4], "d": {"another": "dict"}}
        merged = utils.dictmerge(d1, d2, prefix="*")
        assert merged == {
            "s": "str",
            "i": "* str",
            "l": [1, 2, 3, "* 4"],
            "d": {"another": "* dict"},
        }

        d3 = {"i": "bla", "l": 888, "d": {"another": "thing"}}
        merged = utils.dictmerge(merged, d3, prefix="$")
        assert merged == {
            "s": "str",
            "i": "$ bla",
            "l": [1, 2, 3, "* 4", "$ 888"],
            "d": {"another": "$ thing"},
        }

        d3 = {"l": [8, 9]}
        merged = utils.dictmerge(merged, d3, prefix="^")
        assert merged == {
            "s": "str",
            "i": "$ bla",
            "l": [1, 2, 3, "* 4", "$ 888", "^ 8", "^ 9"],
            "d": {"another": "$ thing"},
        }


class TestPrefixDictValues:
    def test_prefix_dict_values(self):
        d = {
            "s": "str",
            "l": [1, 2, 3],
            "d": {"another": {"yet another": "dict"}},
        }
        prefixed = utils.prefix_dict_values(d, "*")
        assert prefixed == {
            "s": "* str",
            "l": ["* 1", "* 2", "* 3"],
            "d": {"another": {"yet another": "* dict"}},
        }


class TestCleanupCacheDir:
    def test_cleanup_cache_dir(self):
        keychain = mock.Mock()
        keychain.list_orgs.return_value = ["qa", "dev"]
        org = mock.Mock()
        org.config.get.return_value = "http://foo.my.salesforce.com/"
        keychain.get_org.return_value = org
        project_config = mock.Mock()
        with TemporaryDirectory() as temp_for_global:
            keychain.global_config_dir = Path(temp_for_global)
            global_org_dir = _touch_test_org_file(keychain.global_config_dir)
            with TemporaryDirectory() as temp_for_project:
                cache_dir = project_config.cache_dir = Path(temp_for_project)
                project_org_dir = _touch_test_org_file(cache_dir)
                with mock.patch("cumulusci.core.utils.rmtree") as rmtree:
                    cleanup_org_cache_dirs(keychain, project_config)
                    rmtree.assert_has_calls(
                        [mock.call(global_org_dir), mock.call(project_org_dir)],
                        any_order=True,
                    )

    def test_cleanup_cache_dir_nothing_to_cleanup(self):
        keychain = mock.Mock()
        keychain.list_orgs.return_value = ["qa", "dev"]
        org = OrgConfig(
            config={"instance_url": "http://foo.my.salesforce.com/"},
            name="qa",
            keychain=keychain,
            global_org=False,
        )
        keychain.get_org.return_value = org
        project_config = mock.Mock()
        with TemporaryDirectory() as temp_for_global:
            keychain.global_config_dir = Path(temp_for_global)
            with TemporaryDirectory() as temp_for_project:
                cache_dir = project_config.cache_dir = Path(temp_for_project)
                org_dir = cache_dir / "orginfo/foo.my.salesforce.com"
                org_dir.mkdir(parents=True)
                (org_dir / "schema.json").touch()
                with mock.patch("cumulusci.core.utils.rmtree") as rmtree:
                    cleanup_org_cache_dirs(keychain, project_config)
                    assert not rmtree.mock_calls, rmtree.mock_calls


def _touch_test_org_file(directory):
    org_dir = directory / "orginfo/something.something.saleforce.com"
    org_dir.mkdir(parents=True)
    (org_dir / "testfile.json").touch()
    return org_dir
