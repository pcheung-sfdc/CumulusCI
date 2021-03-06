import copy
import random
from pathlib import Path
import tracemalloc
import gc
import sys
from contextlib import contextmanager
import json
from unittest import mock

import responses

from cumulusci.core.config import UniversalConfig
from cumulusci.core.config import BaseProjectConfig
from cumulusci.core.keychain import BaseProjectKeychain
from cumulusci.core.config import OrgConfig
from cumulusci.tasks.bulkdata.tests import utils as bulkdata_utils


def random_sha():
    hash = random.getrandbits(128)
    return "%032x" % hash


def create_project_config(
    repo_name="TestRepo", repo_owner="TestOwner", repo_commit=None
):
    universal_config = UniversalConfig()
    project_config = DummyProjectConfig(
        universal_config=universal_config,
        repo_name=repo_name,
        repo_owner=repo_owner,
        repo_commit=repo_commit,
        config=copy.deepcopy(universal_config.config),
    )
    keychain = BaseProjectKeychain(project_config, None)
    project_config.set_keychain(keychain)
    return project_config


class DummyProjectConfig(BaseProjectConfig):
    def __init__(
        self, universal_config, repo_name, repo_owner, repo_commit=None, config=None
    ):
        repo_info = {
            "owner": repo_owner,
            "name": repo_name,
            "url": f"https://github.com/{repo_owner}/{repo_name}",
            "commit": repo_commit or random_sha(),
        }
        super(DummyProjectConfig, self).__init__(
            universal_config, config, repo_info=repo_info
        )


class DummyOrgConfig(OrgConfig):
    def __init__(self, config=None, name=None, keychain=None, global_org=False):
        if not name:
            name = "test"
        super(DummyOrgConfig, self).__init__(config, name, keychain, global_org)

    def refresh_oauth_token(self, keychain):
        pass


class DummyLogger(object):
    def __init__(self):
        self.out = []

    def log(self, msg, *args):
        self.out.append(msg % args)

    # Compatibility with various logging methods like info, warning, etc
    def __getattr__(self, name):
        return self.log

    def get_output(self):
        return "\n".join(self.out)


class DummyService(object):
    password = "password"

    def __init__(self, name):
        self.name = name


class DummyKeychain(object):
    def get_service(self, name):
        return DummyService(name)

    @property
    def global_config_dir(self):
        return Path.home() / "cumulusci"

    @property
    def cache_dir(self):
        return Path.home() / "project/.cci"


@contextmanager
def assert_max_memory_usage(max_usage):
    "Assert that a test does not exceed a certain memory threshold"
    tracemalloc.start()
    yield
    current, peak = tracemalloc.get_traced_memory()
    if peak > max_usage:
        big_objs(traced_only=True)
        assert peak < max_usage, ("Peak incremental memory usage was high:", peak)
    tracemalloc.stop()


def big_objs(traced_only=False):

    big_objs = (
        (sys.getsizeof(obj), obj)
        for obj in gc.get_objects()
        if sys.getsizeof(obj) > 20000
        and (tracemalloc.get_object_traceback(obj) if traced_only else True)
    )
    for size, obj in big_objs:
        print(type(obj), size, tracemalloc.get_object_traceback(obj))


class FakeSF:
    """Extremely simplistic mock of the Simple-Salesforce API

    Can be improved as needed over time.
    In particular, __getattr__ is not implemented yet.
    """

    fakes = {}

    def describe(self):
        return self._get_json("global_describe")

    @property
    def sf_version(self):
        return "47.0"

    def _get_json(self, fake_dataset):
        self.fakes[fake_dataset] = self.fakes.get("sobjname", None) or read_mock(
            fake_dataset
        )
        return self.fakes[fake_dataset]


def read_mock(name: str):
    base_path = Path(__file__).parent.parent / "tasks/bulkdata/tests"

    with (base_path / f"{name}.json").open("r") as f:
        return f.read()


def mock_describe_calls(domain="example.com"):
    def mock_sobject_describe(name: str):
        responses.add(
            method="GET",
            url=f"https://{domain}/services/data/v48.0/sobjects/{name}/describe",
            body=read_mock(name),
            status=200,
        )

    responses.add(
        method="GET",
        url=f"https://{domain}/services/data",
        body=json.dumps([{"version": "40.0"}, {"version": "48.0"}]),
        status=200,
    )
    responses.add(
        method="GET",
        url=f"https://{domain}/services/data",
        body=json.dumps([{"version": "40.0"}, {"version": "48.0"}]),
        status=200,
    )

    responses.add(
        method="GET",
        url=f"https://{domain}/services/data/v48.0/sobjects",
        body=read_mock("global_describe"),
        status=200,
    )

    for sobject in [
        "Account",
        "Contact",
        "Opportunity",
        "OpportunityContactRole",
        "Case",
    ]:
        mock_sobject_describe(sobject)


@contextmanager
def mock_salesforce_client(task, *, is_person_accounts_enabled=False):
    mock_describe_calls("test.salesforce.com")

    real_init = task._init_task
    salesforce_client = FakeSF()

    def _init_task():
        real_init()
        task.bulk = bulkdata_utils.FakeBulkAPI()
        task.sf = salesforce_client

    with mock.patch(
        "cumulusci.core.config.OrgConfig.is_person_accounts_enabled",
        lambda: is_person_accounts_enabled,
    ), mock.patch.object(task, "_init_task", _init_task):
        yield
