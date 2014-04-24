# Copyright (c) 2012-2013 Mitch Garnaat http://garnaat.org/
# Copyright 2012-2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

import datetime
import functools
import logging
import os

from six.moves import configparser

import botocore.config
from botocore.compat import total_seconds
from botocore.exceptions import UnknownCredentialError
from botocore.exceptions import PartialCredentialsError
from botocore.exceptions import ConfigNotFound
from botocore.utils import InstanceMetadataFetcher, parse_key_val_file


logger = logging.getLogger(__name__)


def create_credential_resolver(session):
    """Create a default credential resolver.

    This creates a pre-configured credential resolver
    that includes the default lookup chain for
    credentials.

    """
    profile_name = session.profile
    scoped_config = session.get_config()
    providers = [
        EnvProvider(),
        SharedCredentialProvider(
            creds_filename=session.get_config_variable('credentials_file'),
            profile_name=profile_name
        ),
        ConfigProvider(config=scoped_config),
        OriginalEC2Provider(),
        BotoProvider(),
        InstanceMetadataProvider(
            iam_role_fetcher=InstanceMetadataFetcher(
                timeout=session.get_config_variable(
                    'metadata_service_timeout'),
                num_attempts=session.get_config_variable(
                    'metadata_service_num_attempts')),
        )
    ]
    resolver = CredentialResolver(providers=providers)
    return resolver


def get_credentials(session):
    resolver = create_credential_resolver(session)
    return resolver.load_credentials()


class Credentials(object):
    """
    Holds the credentials needed to authenticate requests.

    :ivar access_key: The access key part of the credentials.
    :ivar secret_key: The secret key part of the credentials.
    :ivar token: The security token, valid only for session credentials.
    :ivar method: A string which identifies where the credentials
        were found.
    """

    def __init__(self, access_key, secret_key, token=None,
                 method=None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token

        if method is None:
            method = 'explicit'
        self.method = method


class RefreshableCredentials(Credentials):
    """
    Holds the credentials needed to authenticate requests. In addition, it
    knows how to refresh itself.

    :ivar refresh_timeout: How long a given set of credentials are valid for.
        Useful for credentials fetched over the network.
    :ivar access_key: The access key part of the credentials.
    :ivar secret_key: The secret key part of the credentials.
    :ivar token: The security token, valid only for session credentials.
    :ivar method: A string which identifies where the credentials
        were found.
    :ivar session: The ``Session`` the credentials were created for. Useful for
        subclasses.
    """
    refresh_timeout = 15 * 60

    def __init__(self, access_key, secret_key, token,
                 expiry_time, refresh_using, method,
                 time_fetcher=datetime.datetime.utcnow):
        self._refresh_using = refresh_using
        self._access_key = access_key
        self._secret_key = secret_key
        self._token = token
        self._expiry_time = expiry_time
        self._time_fetcher = time_fetcher
        self.method = method

    @classmethod
    def create_from_metadata(cls, metadata, refresh_using, method):
        instance = cls(
            access_key=metadata['access_key'],
            secret_key=metadata['secret_key'],
            token=metadata['token'],
            expiry_time=cls._expiry_datetime(metadata['expiry_time']),
            method=method,
            refresh_using=refresh_using
        )
        return instance

    @property
    def access_key(self):
        self._refresh()
        return self._access_key

    @access_key.setter
    def access_key(self, value):
        self._access_key = value

    @property
    def secret_key(self):
        self._refresh()
        return self._secret_key

    @secret_key.setter
    def secret_key(self, value):
        self._secret_key = value

    @property
    def token(self):
        self._refresh()
        return self._token

    @token.setter
    def token(self, value):
        self._token = value

    def _seconds_remaining(self):
        delta = self._expiry_time - self._time_fetcher()
        return total_seconds(delta)

    def refresh_needed(self):
        if self._expiry_time is None:
            # No expiration, so assume we don't need to refresh.
            return False

        # The credentials should be refreshed if they're going to expire
        # in less than 5 minutes.
        if self._seconds_remaining() >= self.refresh_timeout:
            # There's enough time left. Don't refresh.
            return False

        # Assume the worst & refresh.
        logger.debug("Credentials need to be refreshed.")
        return True

    def _refresh(self):
        if not self.refresh_needed():
            return

        metadata = self._refresh_using()
        self._set_from_data(metadata)

    @staticmethod
    def _expiry_datetime(time_str):
        return datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")

    def _set_from_data(self, data):
        self.access_key = data['access_key']
        self.secret_key = data['secret_key']
        self.token = data['token']
        self._expiry_time = datetime.datetime.strptime(
            data['expiry_time'],
            "%Y-%m-%dT%H:%M:%SZ"
        )
        logger.debug("Retrieved credentials will expire at: %s", self._expiry_time)


class CredentialProvider(object):

    # Implementations must provide a method.
    METHOD = None

    def __init__(self, session=None):
        self.session = session

    def load(self):
        """
        Loads the credentials from their source & sets them on the object.

        Subclasses should implement this method (by reading from disk, the
        environment, the network or wherever), returning ``True`` if they were
        found & loaded.

        If not found, this method should return ``False``, indictating that the
        ``CredentialResolver`` should fall back to the next available method.

        The default implementation does nothing, assuming the user has set the
        ``access_key/secret_key/token`` themselves.

        :returns: Whether credentials were found & set
        :rtype: boolean
        """
        return True


class InstanceMetadataProvider(CredentialProvider):
    METHOD = 'iam-role'

    def __init__(self, iam_role_fetcher):
        self._role_fetcher = iam_role_fetcher

    def load(self):
        fetcher = self._role_fetcher
        # We do the first request, to see if we get useful data back.
        # If not, we'll pass & move on to whatever's next in the credential
        # chain.
        metadata = fetcher.retrieve_iam_role_credentials()
        if not metadata:
            return None
        logger.info('Found credentials from IAM Role: %s', metadata['role_name'])
        # We manually set the data here, since we already made the request &
        # have it. When the expiry is hit, the credentials will auto-refresh
        # themselves.
        creds = RefreshableCredentials.create_from_metadata(
            metadata,
            method=self.METHOD,
            refresh_using=fetcher.retrieve_iam_role_credentials,
        )
        return creds


class EnvProvider(CredentialProvider):
    METHOD = 'env'
    ACCESS_KEY = 'AWS_ACCESS_KEY_ID'
    SECRET_KEY = 'AWS_SECRET_ACCESS_KEY'
    # The token can come from either of these env var.
    # AWS_SESSION_TOKEN is what other AWS SDKs have standardized on.
    TOKENS = ['AWS_SECURITY_TOKEN', 'AWS_SESSION_TOKEN']

    def __init__(self, environ=None):
        if environ is None:
            environ = os.environ
        self.environ = environ

    def load(self):
        """
        Search for credentials in explicit environment variables.
        """
        if self.ACCESS_KEY in self.environ:
            logger.info('Found credentials in Environment variables.')
            access_key = self.environ[self.ACCESS_KEY]
            secret_key = self.environ[self.SECRET_KEY]
            token = self._get_session_token()
            return Credentials(access_key, secret_key, token,
                               method=self.METHOD)
        else:
            return None

    def _get_session_token(self):
        for token_envvar in self.TOKENS:
            if token_envvar in self.environ:
                return self.environ[token_envvar]


class OriginalEC2Provider(CredentialProvider):
    METHOD = 'ec2-credentials-file'

    CRED_FILE_ENV = 'AWS_CREDENTIAL_FILE'
    ACCESS_KEY = 'AWSAccessKeyId'
    SECRET_KEY = 'AWSSecretKey'

    def __init__(self, environ=None, parser=None):
        if environ is None:
            environ = os.environ
        if parser is None:
            parser = parse_key_val_file
        self._environ = environ
        self._parser = parser

    def load(self):
        """
        Search for a credential file used by original EC2 CLI tools.
        """
        if 'AWS_CREDENTIAL_FILE' in self._environ:
            full_path = os.path.expanduser(self._environ['AWS_CREDENTIAL_FILE'])
            creds = self._parser(full_path)
            if self.ACCESS_KEY in creds:
                logger.info('Found credentials in AWS_CREDENTIAL_FILE.')
                access_key = creds[self.ACCESS_KEY]
                secret_key = creds[self.SECRET_KEY]
                # EC2 creds file doesn't support session tokens.
                return Credentials(access_key, secret_key, method=self.METHOD)
        else:
            return None


class SharedCredentialProvider(CredentialProvider):
    METHOD = 'shared-credentials-file'

    ACCESS_KEY = 'aws_access_key_id'
    SECRET_KEY = 'aws_secret_access_key'
    # Same deal as the EnvProvider above.  Botocore originally supported
    # aws_security_token, but the SDKs are standardizing on aws_session_token
    # so we support both.
    TOKENS = ['aws_security_token', 'aws_session_token']

    def __init__(self, creds_filename, profile_name=None, ini_parser=None):
        self._creds_filename = creds_filename
        if profile_name is None:
            profile_name = 'default'
        self._profile_name = profile_name
        if ini_parser is None:
            ini_parser = botocore.config.get_config
        self._ini_parser = ini_parser

    def load(self):
        try:
            available_creds = self._ini_parser(self._creds_filename)
        except ConfigNotFound:
            return None
        if self._profile_name in available_creds:
            config = available_creds[self._profile_name]
            if self.ACCESS_KEY in config:
                logger.info("Found credentials in shared credentials file: %s",
                            self._creds_filename)
                access_key = config[self.ACCESS_KEY]
                secret_key = config[self.SECRET_KEY]
                token =  self._get_session_token(config)
                return Credentials(access_key, secret_key, token,
                                   method=self.METHOD)

    def _get_session_token(self, config):
        for token_envvar in self.TOKENS:
            if token_envvar in config:
                return config[token_envvar]


class ConfigProvider(CredentialProvider):
    METHOD = 'config-file'

    ACCESS_KEY = 'aws_access_key_id'
    SECRET_KEY = 'aws_secret_access_key'
    # Same deal as the EnvProvider above.  Botocore originally supported
    # aws_security_token, but the SDKs are standardizing on aws_session_token
    # so we support both.
    TOKENS = ['aws_security_token', 'aws_session_token']

    def __init__(self, config):
        """

        :param config: The session configuration scoped to the current profile.
            This is available via ``session.config``.

        """
        self._config = config

    def load(self):
        """
        If there is are credentials in the configuration associated with
        the session, use those.
        """
        if self.ACCESS_KEY in self._config:
            logger.info("Credentials found in config file.")
            access_key = self._config[self.ACCESS_KEY]
            secret_key = self._config[self.SECRET_KEY]
            token = self._get_session_token()
            return Credentials(access_key, secret_key, token,
                               method=self.METHOD)
        else:
            return None

    def _get_session_token(self):
        for token_name in self.TOKENS:
            if token_name in self._config:
                return self._config[token_name]


class BotoProvider(CredentialProvider):
    METHOD = 'boto-config'

    BOTO_CONFIG_ENV = 'BOTO_CONFIG'
    DEFAULT_CONFIG_FILENAMES = ['/etc/boto.cfg', '~/.boto']
    ACCESS_KEY = 'aws_access_key_id'
    SECRET_KEY = 'aws_secret_access_key'

    def __init__(self, environ=None, ini_parser=None):
        if environ is None:
            environ = os.environ
        if ini_parser is None:
            ini_parser = botocore.config.get_config
        self._environ = environ
        self._ini_parser = ini_parser

    def load(self):
        """
        Look for credentials in boto config file.
        """
        if self.BOTO_CONFIG_ENV in self._environ:
            potential_locations = [self._environ[self.BOTO_CONFIG_ENV]]
        else:
            potential_locations = self.DEFAULT_CONFIG_FILENAMES
        for filename in potential_locations:
            try:
                config = self._ini_parser(filename)
            except ConfigNotFound:
                # Move on to the next potential config file name.
                continue
            if 'Credentials' in config:
                credentials = config['Credentials']
                if self.ACCESS_KEY in credentials:
                    logger.info("Found credentials in boto config file: %s",
                                filename)
                    access_key = credentials[self.ACCESS_KEY]
                    secret_key = credentials[self.SECRET_KEY]
                    return Credentials(access_key, secret_key,
                                       method=self.METHOD)


# TODO: cache?
class CredentialResolver(object):

    def __init__(self, providers):
        """

        :param providers: A list of ``CredentialProvider`` instances.

        """
        self.providers = providers

    def insert_before(self, name, credential_provider):
        """
        Inserts a new instance of ``CredentialProvider`` into the chain that will
        be tried before an existing one.

        :param name: The short name of the credentials you'd like to insert the
            new credentials before. (ex. ``env`` or ``config``). Existing names
            & ordering can be discovered via ``self.available_methods``.
        :type name: string

        :param cred_instance: An instance of the new ``Credentials`` object
            you'd like to add to the chain.
        :type cred_instance: A subclass of ``Credentials``
        """
        try:
            offset = self.available_methods.index(name)
        except ValueError:
            raise UnknownCredentialError(name=name)

        self.methods.insert(offset, cred_instance)
        self._rebuild_available_methods()

    def insert_after(self, name, cred_instance):
        """
        Inserts a new type of ``Credentials`` instance into the chain that will
        be tried after an existing one.

        :param name: The short name of the credentials you'd like to insert the
            new credentials after. (ex. ``env`` or ``config``). Existing names
            & ordering can be discovered via ``self.available_methods``.
        :type name: string

        :param cred_instance: An instance of the new ``Credentials`` object
            you'd like to add to the chain.
        :type cred_instance: A subclass of ``Credentials``
        """
        try:
            offset = [p.METHOD for p in self.providers].index(name)
        except ValueError:
            raise UnknownCredentialError(name=name)

        self.providers.insert(offset + 1, cred_instance)

    def remove(self, name):
        """
        Removes a given ``Credentials`` instance from the chain.

        :param name: The short name of the credentials instance to remove.
        :type name: string
        """
        available_methods = [p.METHOD for p in self.providers]
        if not name in available_methods:
            # It's not present. Fail silently.
            return

        offset = available_methods.index(name)
        self.providers.pop(offset)

    def load_credentials(self):
        """
        Goes through the credentials chain, returning the first ``Credentials``
        that could be loaded.
        """
        # First provider to return a non-None response wins.
        for provider in self.providers:
            creds = provider.load()
            if creds is not None:
                return creds

        # If we got here, no credentials could be found.
        # This feels like it should be an exception, but historically, ``None``
        # is returned.
        # 
        # +1
        # -js
        return None
