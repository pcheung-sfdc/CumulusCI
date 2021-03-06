from collections import defaultdict
from collections import namedtuple
from distutils.version import StrictVersion
import os
import re
from contextlib import contextmanager
from urllib.parse import urlparse
from cumulusci.utils.fileutils import open_fs_resource

import requests
from simple_salesforce import Salesforce

from cumulusci.core.config import BaseConfig
from cumulusci.core.exceptions import CumulusCIException
from cumulusci.core.exceptions import DependencyResolutionError
from cumulusci.core.exceptions import SalesforceCredentialsException
from cumulusci.oauth.salesforce import SalesforceOAuth2
from cumulusci.oauth.salesforce import jwt_session


SKIP_REFRESH = os.environ.get("CUMULUSCI_DISABLE_REFRESH")
SANDBOX_MYDOMAIN_RE = re.compile(r"\.cs\d+\.my\.(.*)salesforce\.com")
MYDOMAIN_RE = re.compile(r"\.my\.(.*)salesforce\.com")


VersionInfo = namedtuple("VersionInfo", ["id", "number"])


class OrgConfig(BaseConfig):
    """ Salesforce org configuration (i.e. org credentials) """

    # make sure it can be mocked for tests
    SalesforceOAuth2 = SalesforceOAuth2

    def __init__(self, config: dict, name: str, keychain=None, global_org=False):
        self.keychain = keychain
        self.global_org = global_org

        self.name = name
        self._community_info_cache = {}
        self._latest_api_version = None
        self._installed_packages = None
        self._is_person_accounts_enabled = None
        super(OrgConfig, self).__init__(config)

    def refresh_oauth_token(self, keychain, connected_app=None):
        """Get a fresh access token and store it in the org config.

        If the SFDX_CLIENT_ID and SFDX_HUB_KEY environment variables are set,
        this is done using the Oauth2 JWT flow.

        Otherwise it is done using the Oauth2 Refresh Token flow using the connected app
        configured in the keychain's connected_app service.

        Also refreshes user and org info that is cached in the org config.
        """
        if not SKIP_REFRESH:
            SFDX_CLIENT_ID = os.environ.get("SFDX_CLIENT_ID")
            SFDX_HUB_KEY = os.environ.get("SFDX_HUB_KEY")
            if SFDX_CLIENT_ID and SFDX_HUB_KEY:
                info = jwt_session(
                    SFDX_CLIENT_ID,
                    SFDX_HUB_KEY,
                    self.username,
                    self.instance_url,
                    auth_url=self.id,
                )
            else:
                info = self._refresh_token(keychain, connected_app)
            if info != self.config:
                self.config.update(info)
        self._load_userinfo()
        self._load_orginfo()

    @contextmanager
    def save_if_changed(self):
        orig_config = self.config.copy()
        yield
        if self.config != orig_config:
            self.logger.info("Org info updated, writing to keychain")
            self.save()

    def _refresh_token(self, keychain, connected_app):
        if keychain:  # it might be none'd and caller adds connected_app
            connected_app = keychain.get_service("connected_app")
        if connected_app is None:
            raise AttributeError(
                "No connected app or keychain was passed to refresh_oauth_token."
            )
        client_id = self.client_id
        client_secret = self.client_secret
        if not client_id:
            client_id = connected_app.client_id
            client_secret = connected_app.client_secret
        sf_oauth = self.SalesforceOAuth2(
            client_id,
            client_secret,
            connected_app.callback_url,  # Callback url isn't really used for this call
            auth_site=self.instance_url,
        )

        resp = sf_oauth.refresh_token(self.refresh_token)
        if resp.status_code != 200:
            raise SalesforceCredentialsException(
                f"Error refreshing OAuth token: {resp.text}"
            )
        return resp.json()

    @property
    def lightning_base_url(self):
        instance_url = self.instance_url.rstrip("/")
        if SANDBOX_MYDOMAIN_RE.search(instance_url):
            return SANDBOX_MYDOMAIN_RE.sub(r".lightning.\1force.com", instance_url)
        elif MYDOMAIN_RE.search(instance_url):
            return MYDOMAIN_RE.sub(r".lightning.\1force.com", instance_url)
        else:
            return self.instance_url.split(".")[0] + ".lightning.force.com"

    @property
    def salesforce_client(self):
        return Salesforce(
            instance=self.instance_url.replace("https://", ""),
            session_id=self.access_token,
            version=self.latest_api_version,
        )

    @property
    def latest_api_version(self):
        if not self._latest_api_version:
            headers = {"Authorization": "Bearer " + self.access_token}
            response = requests.get(
                self.instance_url + "/services/data", headers=headers
            )
            self._latest_api_version = str(response.json()[-1]["version"])

        return self._latest_api_version

    @property
    def start_url(self):
        start_url = "%s/secur/frontdoor.jsp?sid=%s" % (
            self.instance_url,
            self.access_token,
        )
        return start_url

    @property
    def user_id(self):
        return self.id.split("/")[-1]

    @property
    def org_id(self):
        return self.id.split("/")[-2]

    @property
    def username(self):
        """ Username for the org connection. """
        username = self.config.get("username")
        if not username:
            username = self.userinfo__preferred_username
        return username

    def load_userinfo(self):
        self._load_userinfo()

    def _load_userinfo(self):
        headers = {"Authorization": "Bearer " + self.access_token}
        response = requests.get(
            self.instance_url + "/services/oauth2/userinfo", headers=headers
        )
        if response != self.config.get("userinfo", {}):
            self.config.update({"userinfo": response.json()})

    def can_delete(self):
        return False

    def _load_orginfo(self):
        self._org_sobject = self.salesforce_client.Organization.get(self.org_id)

        result = {
            "org_type": self._org_sobject["OrganizationType"],
            "is_sandbox": self._org_sobject["IsSandbox"],
            "instance_name": self._org_sobject["InstanceName"],
        }
        self.config.update(result)

    @property
    def organization_sobject(self):
        return self._org_sobject

    def _fetch_community_info(self):
        """Use the API to re-fetch information about communities"""
        response = self.salesforce_client.restful("connect/communities")

        # Since community names must be unique, we'll return a dictionary
        # with the community names as keys
        result = {community["name"]: community for community in response["communities"]}
        return result

    def get_community_info(self, community_name, force_refresh=False):
        """Return the community information for the given community

        An API call will be made the first time this function is used,
        and the return values will be cached. Subsequent calls will
        not call the API unless the requested community name is not in
        the cached results, or unless the force_refresh parameter is
        set to True.

        """

        if force_refresh or community_name not in self._community_info_cache:
            self._community_info_cache = self._fetch_community_info()

        if community_name not in self._community_info_cache:
            raise Exception(
                f"Unable to find community information for '{community_name}'"
            )

        return self._community_info_cache[community_name]

    def has_minimum_package_version(self, package_identifier, version_identifier):
        """Return True if the org has a version of the specified package that is
        equal to or newer than the supplied version identifier.

        The package identifier may be either a namespace or a 033 package Id.
        The version identifier should be in "1.2.3" or "1.2.3b4" format.

        A CumulusCIException will be thrown if you request to check a namespace
        and multiple second-generation packages sharing that namespace are installed.
        Use a package Id to handle this circumstance."""
        installed_version = self.installed_packages.get(package_identifier)

        if not installed_version:
            return False
        elif len(installed_version) > 1:
            raise CumulusCIException(
                f"Cannot check installed version of {package_identifier}, because multiple "
                f"packages are installed that match this identifier."
            )

        return installed_version[0].number >= version_identifier

    @property
    def installed_packages(self):
        """installed_packages is a dict mapping a namespace or package Id (033*) to the installed package
        version(s) matching that identifier. All values are lists, because multiple second-generation
        packages may be installed with the same namespace.

        To check if a required package is present, call `has_minimum_package_version()` with either the
        namespace or 033 Id of the desired package and its version, in 1.2.3 format.

        Beta version of a package are represented as "1.2.3b5", where 5 is the build number."""
        if self._installed_packages is None:
            isp_result = self.salesforce_client.restful(
                "tooling/query/?q=SELECT SubscriberPackage.Id, SubscriberPackage.NamespacePrefix, "
                "SubscriberPackageVersionId FROM InstalledSubscriberPackage"
            )
            _installed_packages = defaultdict(list)
            for isp in isp_result["records"]:
                sp = isp["SubscriberPackage"]
                spv_result = self.salesforce_client.restful(
                    "tooling/query/?q=SELECT Id, MajorVersion, MinorVersion, PatchVersion, BuildNumber, "
                    f"IsBeta FROM SubscriberPackageVersion WHERE Id='{isp['SubscriberPackageVersionId']}'"
                )
                if not spv_result["records"]:
                    # This _shouldn't_ happen, but it is possible in customer orgs.
                    continue
                spv = spv_result["records"][0]

                version = f"{spv['MajorVersion']}.{spv['MinorVersion']}"
                if spv["PatchVersion"]:
                    version += f".{spv['PatchVersion']}"
                if spv["IsBeta"]:
                    version += f"b{spv['BuildNumber']}"
                version_info = VersionInfo(spv["Id"], StrictVersion(version))
                namespace = sp["NamespacePrefix"]
                _installed_packages[namespace].append(version_info)
                namespace_version = f"{namespace}@{version}"
                _installed_packages[namespace_version].append(version_info)
                _installed_packages[sp["Id"]].append(version_info)

            self._installed_packages = _installed_packages
        return self._installed_packages

    def reset_installed_packages(self):
        self._installed_packages = None

    def save(self):
        assert self.keychain, "Keychain was not set on OrgConfig"
        self.keychain.set_org(self, self.global_org)

    def get_domain(self):
        instance_url = self.config.get("instance_url", "")
        return urlparse(instance_url).hostname or ""

    def get_orginfo_cache_dir(self, cachename):
        "Returns a context managed FSResource object"
        assert self.keychain, "Keychain should be set"
        if self.global_org:
            cache_dir = self.keychain.global_config_dir
        else:
            cache_dir = self.keychain.cache_dir
        uniqifier = self.get_domain() + "__" + str(self.username).replace("@", "__")
        cache_dir = cache_dir / "orginfo" / uniqifier / cachename

        cache_dir.mkdir(parents=True, exist_ok=True)
        return open_fs_resource(cache_dir)

    @property
    def is_person_accounts_enabled(self):
        """
        Returns if the org has person accounts enabled, i.e. if Account has an ``IsPersonAccount`` field.

        **Example**

        Selectively run a task in a flow only if Person Accounts is or is not enabled.

        .. code-block:: yaml

            flows:
                load_storytelling_data:
                    steps:
                        1:
                            task: load_dataset
                            options:
                                mapping: datasets/with_person_accounts/mapping.yml
                                sql_path: datasets/with_person_accounts/data.sql
                            when: org_config.is_person_accounts_enabled
                        2:
                            task: load_dataset
                            options:
                                mapping: datasets/without_person_accounts/mapping.yml
                                sql_path: datasets/without_person_accounts/data.sql
                            when: not org_config.is_person_accounts_enabled

        """
        if self._is_person_accounts_enabled is None:
            self._is_person_accounts_enabled = any(
                field["name"] == "IsPersonAccount"
                for field in self.salesforce_client.Account.describe()["fields"]
            )
        return self._is_person_accounts_enabled

    def resolve_04t_dependencies(self, dependencies):
        """Look up 04t SubscriberPackageVersion ids for 1gp project dependencies"""
        new_dependencies = []
        for dependency in dependencies:
            dependency = {**dependency}

            if "namespace" in dependency:
                # get the SubscriberPackageVersion id
                key = f"{dependency['namespace']}@{dependency['version']}"
                version_info = self.installed_packages.get(key)
                if version_info:
                    dependency["version_id"] = version_info[0].id
                else:
                    raise DependencyResolutionError(
                        f"Could not find 04t id for package {key} in org {self.name}"
                    )

            # recurse
            if "dependencies" in dependency:
                dependency["dependencies"] = self.resolve_04t_dependencies(
                    dependency["dependencies"]
                )

            new_dependencies.append(dependency)
        return new_dependencies
