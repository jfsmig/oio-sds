# Copyright (C) 2015-2020 OpenIO SAS, as part of OpenIO SDS
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.

"""Container-related commands"""

from six import iteritems
from logging import getLogger

from oio.cli import Command, Lister, ShowOne
from oio.common.exceptions import NoSuchContainer
from oio.common.timestamp import Timestamp
from oio.common.utils import depaginate, request_id, timeout_to_deadline
from oio.common.constants import \
    BUCKET_PROP_REPLI_ENABLED, \
    OIO_DB_STATUS_NAME, OIO_DB_ENABLED, OIO_DB_DISABLED, OIO_DB_FROZEN, \
    M2_PROP_BUCKET_NAME, M2_PROP_CTIME, M2_PROP_DAMAGED_OBJECTS, \
    M2_PROP_DEL_EXC_VERSIONS, M2_PROP_MISSING_CHUNKS, \
    M2_PROP_OBJECTS, M2_PROP_QUOTA, M2_PROP_STORAGE_POLICY, \
    M2_PROP_USAGE, M2_PROP_VERSIONING_POLICY
from oio.common.easy_value import boolean_value


class SetPropertyCommandMixin(object):
    """Command setting quota, storage policy or generic property"""

    def patch_parser(self, parser):
        from oio.cli.common.utils import KeyValueAction

        parser.add_argument(
            '--property',
            metavar='<key=value>',
            action=KeyValueAction,
            help='Property to add/update for the container(s)'
        )
        parser.add_argument(
            '--quota',
            metavar='<bytes>',
            type=int,
            help='Set the quota on the container'
        )
        parser.add_argument(
            '--storage-policy', '--stgpol',
            metavar='<storage_policy>',
            help='Set the storage policy of the container'
        )
        parser.add_argument(
            '--max-versions', '--versioning',
            metavar='<n>',
            type=int,
            help="""Set the versioning policy of the container.
 n<0 is unlimited number of versions.
 n=0 is disabled (cannot overwrite existing object).
 n=1 is suspended (can overwrite existing object).
 n>1 is maximum n versions.
"""
        )
        parser.add_argument(
            '--delete-exceeding-versions',
            metavar='<bool>',
            type=boolean_value,
            help="""Delete exceeding versions when adding a new object
 (only if versioning is enabled).
"""
        )


class ContainerCommandMixin(object):
    """Command taking a container or CID as parameter"""

    def patch_parser_container(self, parser):
        parser.add_argument(
            '--cid',
            dest='is_cid',
            default=False,
            help="Interpret container as a CID",
            action='store_true'
        )
        parser.add_argument(
            'container',
            metavar='<container>',
            help=("Name or CID of the container to interact with.\n")
        )

    def take_action_container(self, parsed_args):
        parsed_args.cid = None
        if parsed_args.is_cid:
            parsed_args.cid = parsed_args.container
            parsed_args.container = None


class ContainersCommandMixin(object):
    """Command taking some containers or CIDs as parameter"""

    def patch_parser_container(self, parser):
        parser.add_argument(
            '--cid',
            dest='is_cid',
            default=False,
            help="Interpret containers as a CID",
            action='store_true'
        )
        parser.add_argument(
            'containers',
            metavar='<containers>',
            nargs='+',
            help=("Names or CIDs of the containers to interact with.\n")
        )


class CreateContainer(SetPropertyCommandMixin, Lister):
    """Create an object container."""

    log = getLogger(__name__ + '.CreateContainer')

    def get_parser(self, prog_name):
        parser = super(CreateContainer, self).get_parser(prog_name)
        self.patch_parser(parser)
        parser.add_argument(
            '--bucket-name',
            help=('Declare the container belongs to the specified bucket')
        )
        parser.add_argument(
            'containers',
            metavar='<container-name>',
            nargs='+',
            help='New container name(s)'
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        properties = parsed_args.property
        system = dict()
        if parsed_args.bucket_name:
            system[M2_PROP_BUCKET_NAME] = parsed_args.bucket_name
        if parsed_args.quota is not None:
            system[M2_PROP_QUOTA] = str(parsed_args.quota)
        if parsed_args.storage_policy is not None:
            system[M2_PROP_STORAGE_POLICY] = parsed_args.storage_policy
        if parsed_args.max_versions is not None:
            system[M2_PROP_VERSIONING_POLICY] = str(parsed_args.max_versions)
        if parsed_args.delete_exceeding_versions is not None:
            system[M2_PROP_DEL_EXC_VERSIONS] = \
                str(int(parsed_args.delete_exceeding_versions))

        results = []
        account = self.app.client_manager.account
        if len(parsed_args.containers) > 1:
            results = self.app.client_manager.storage.container_create_many(
                account,
                parsed_args.containers,
                properties=properties,
                system=system)

        else:
            for container in parsed_args.containers:
                success = self.app.client_manager.storage.container_create(
                    account,
                    container,
                    properties=properties,
                    system=system)
                results.append((container, success))

        return ('Name', 'Created'), (r for r in results)


class SetBucket(ShowOne):
    """Set metadata on a bucket."""

    log = getLogger(__name__ + '.SetBucket')

    def get_parser(self, prog_name):
        parser = super(SetBucket, self).get_parser(prog_name)
        parser.add_argument(
            'bucket',
            help='Name of the bucket to query.'
        )
        parser.add_argument(
            '--replicate', '--replication',
            metavar='yes/no',
            type=boolean_value,
            help='Enable or disable bucket replication.'
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        reqid = request_id(prefix='CLI-BUCKET-')
        acct_client = self.app.client_manager.storage.account
        metadata = dict()
        if parsed_args.replicate is not None:
            metadata[BUCKET_PROP_REPLI_ENABLED] = str(parsed_args.replicate)
        data = acct_client.bucket_update(parsed_args.bucket,
                                         metadata=metadata, to_delete=None,
                                         reqid=reqid)

        if parsed_args.formatter == 'table':
            from oio.common.easy_value import convert_size

            data['bytes'] = convert_size(data['bytes'])
            data['mtime'] = Timestamp(data.get('mtime', 0.0)).isoformat
        return zip(*sorted(data.items()))


class SetContainer(SetPropertyCommandMixin,
                   ContainerCommandMixin, Command):
    """
    Set container properties, quota, storage policy, status or versioning.
    """

    log = getLogger(__name__ + '.SetContainer')

    def get_parser(self, prog_name):
        parser = super(SetContainer, self).get_parser(prog_name)
        self.patch_parser(parser)
        self.patch_parser_container(parser)
        # Same as in CreateContainer class
        parser.add_argument(
            '--bucket-name',
            help=('Declare the container belongs to the specified bucket')
        )
        parser.add_argument(
            '--clear',
            dest='clear',
            default=False,
            help='Clear previous properties',
            action="store_true"
        )
        parser.add_argument(
            '--status',
            metavar='<status>',
            help='Set container status, can be enabled, disabled or frozen'
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        super(SetContainer, self).take_action_container(parsed_args)
        properties = parsed_args.property
        system = dict()
        if parsed_args.bucket_name:
            system[M2_PROP_BUCKET_NAME] = parsed_args.bucket_name
        if parsed_args.quota is not None:
            system[M2_PROP_QUOTA] = str(parsed_args.quota)
        if parsed_args.storage_policy is not None:
            system[M2_PROP_STORAGE_POLICY] = parsed_args.storage_policy
        if parsed_args.max_versions is not None:
            system[M2_PROP_VERSIONING_POLICY] = str(parsed_args.max_versions)
        if parsed_args.delete_exceeding_versions is not None:
            system[M2_PROP_DEL_EXC_VERSIONS] = \
                str(int(parsed_args.delete_exceeding_versions))
        if parsed_args.status is not None:
            status_value = {
                'enabled': str(OIO_DB_ENABLED),
                'disabled': str(OIO_DB_DISABLED),
                'frozen': str(OIO_DB_FROZEN)
            }
            system['sys.status'] = status_value[parsed_args.status]

        self.app.client_manager.storage.container_set_properties(
            self.app.client_manager.account,
            parsed_args.container,
            properties,
            clear=parsed_args.clear,
            system=system,
            cid=parsed_args.cid
        )


class TouchContainer(ContainersCommandMixin, Command):
    """Touch an object container, triggers asynchronous treatments on it."""

    log = getLogger(__name__ + '.TouchContainer')

    def get_parser(self, prog_name):
        parser = super(TouchContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        parser.add_argument(
            '--recompute',
            dest='recompute',
            default=False,
            help='Recompute the statistics of the specified container',
            action="store_true"
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        if parsed_args.is_cid:
            for container in parsed_args.containers:
                self.app.client_manager.storage.container_touch(
                    self.app.client_manager.account,
                    None, recompute=parsed_args.recompute, cid=container
                )
        else:
            for container in parsed_args.containers:
                self.app.client_manager.storage.container_touch(
                    self.app.client_manager.account,
                    container, recompute=parsed_args.recompute
                )


class DeleteContainer(ContainersCommandMixin, Command):
    """Delete an object container."""

    log = getLogger(__name__ + '.DeleteContainer')

    def get_parser(self, prog_name):
        parser = super(DeleteContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        if parsed_args.is_cid:
            for container in parsed_args.containers:
                self.app.client_manager.storage.container_delete(
                    self.app.client_manager.account,
                    None, cid=container
                )
        else:
            for container in parsed_args.containers:
                self.app.client_manager.storage.container_delete(
                    self.app.client_manager.account,
                    container
                )


class FlushContainer(ContainerCommandMixin, Command):
    """Flush an object container."""

    log = getLogger(__name__ + '.FlushContainer')

    def get_parser(self, prog_name):
        parser = super(FlushContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        parser.add_argument(
            '--quickly',
            action='store_true',
            dest='quick',
            help="""Flush container quickly, may put high pressure
 on the event system"""
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        self.take_action_container(parsed_args)
        if parsed_args.cid is None:
            account = self.app.client_manager.account
            container = parsed_args.container
        else:
            account, container = \
                self.app.client_manager.storage.resolve_cid(
                    parsed_args.cid)
        self.app.client_manager.storage.container_flush(
            account, container, fast=parsed_args.quick)


class ShowBucket(ShowOne):
    """Display information about a bucket."""

    log = getLogger(__name__ + '.ShowBucket')

    def get_parser(self, prog_name):
        parser = super(ShowBucket, self).get_parser(prog_name)
        parser.add_argument(
            'bucket',
            help='Name of the bucket to query.'
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        reqid = request_id(prefix='CLI-BUCKET-')
        acct_client = self.app.client_manager.storage.account
        data = acct_client.bucket_show(parsed_args.bucket, reqid=reqid)

        if parsed_args.formatter == 'table':
            from oio.common.easy_value import convert_size

            data['bytes'] = convert_size(data['bytes'])
            data['mtime'] = Timestamp(data.get('mtime', 0.0)).isoformat
        return zip(*sorted(data.items()))


class ShowContainer(ContainerCommandMixin, ShowOne):
    """Display information about an object container."""

    log = getLogger(__name__ + '.ShowContainer')

    def get_parser(self, prog_name):
        parser = super(ShowContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        account = self.app.client_manager.account
        self.take_action_container(parsed_args)
        # The command is named 'show' but we must call
        # container_get_properties() because container_show() does
        # not return system properties (and we need them).
        data = self.app.client_manager.storage.container_get_properties(
            account,
            parsed_args.container,
            cid=parsed_args.cid
        )

        sys = data['system']
        ctime = float(sys[M2_PROP_CTIME]) / 1000000.
        bytes_usage = sys.get(M2_PROP_USAGE, 0)
        objects = sys.get(M2_PROP_OBJECTS, 0)
        damaged_objects = sys.get(M2_PROP_DAMAGED_OBJECTS, 0)
        missing_chunks = sys.get(M2_PROP_MISSING_CHUNKS, 0)
        if parsed_args.formatter == 'table':
            from oio.common.easy_value import convert_size

            ctime = int(ctime)
            bytes_usage = convert_size(int(bytes_usage), unit="B")
            objects = convert_size(int(objects))
        info = {
            'account': sys['sys.account'],
            'base_name': sys['sys.name'],
            'container': sys['sys.user.name'],
            'ctime': ctime,
            'bytes_usage': bytes_usage,
            'quota': sys.get(M2_PROP_QUOTA, "Namespace default"),
            'objects': objects,
            'damaged_objects': damaged_objects,
            'missing_chunks': missing_chunks,
            'storage_policy': sys.get(M2_PROP_STORAGE_POLICY,
                                      "Namespace default"),
            'max_versions': sys.get(M2_PROP_VERSIONING_POLICY,
                                    "Namespace default"),
            'status': OIO_DB_STATUS_NAME.get(sys.get('sys.status'), "Unknown"),
        }
        bucket = sys.get(M2_PROP_BUCKET_NAME, None)
        if bucket is not None:
            info['bucket'] = bucket
        delete_exceeding = sys.get(M2_PROP_DEL_EXC_VERSIONS, None)
        if delete_exceeding is not None:
            info['delete_exceeding_versions'] = delete_exceeding != '0'
        for k, v in iteritems(data['properties']):
            info['meta.' + k] = v
        return list(zip(*sorted(info.items())))


class ListBuckets(Lister):
    """Get the list of buckets owned by an account."""

    log = getLogger(__name__ + '.ListBuckets')

    def get_parser(self, prog_name):
        from oio.cli.common.utils import ValueFormatStoreTrueAction

        parser = super(ListBuckets, self).get_parser(prog_name)
        parser.add_argument(
            '--prefix',
            metavar='<prefix>',
            help='Filter the list using <prefix>'
        )
        parser.add_argument(
            '--marker',
            metavar='<marker>',
            help='Marker for paging'
        )
        parser.add_argument(
            '--limit',
            metavar='<limit>',
            help='Limit the number of buckets returned'
        )
        parser.add_argument(
            '--no-paging', '--full',
            dest='full_listing',
            default=False,
            help=("List all buckets without paging "
                  "(and set output format to 'value')"),
            action=ValueFormatStoreTrueAction
        )
        parser.add_argument(
            '--versioning',
            action='store_true',
            dest='versioning',
            help="Display the versioning state of each bucket"
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        kwargs = {'reqid': request_id(prefix='CLI-BUCKET-')}
        if parsed_args.prefix:
            kwargs['prefix'] = parsed_args.prefix
        if parsed_args.marker:
            kwargs['marker'] = parsed_args.marker
        if parsed_args.limit:
            kwargs['limit'] = parsed_args.limit

        account = self.app.client_manager.account
        acct_client = self.app.client_manager.storage.account
        storage = self.app.client_manager.storage

        if parsed_args.full_listing:
            listing = depaginate(
                acct_client.bucket_list,
                listing_key=lambda x: x['listing'],
                marker_key=lambda x: x.get('next_marker'),
                truncated_key=lambda x: x['truncated'],
                account=account,
                **kwargs)
        else:
            acct_meta = acct_client.bucket_list(account, **kwargs)
            listing = acct_meta['listing']

        columns = ('Name', 'Bytes', 'Objects', 'Mtime', )

        def versioning(bucket):
            try:
                data = storage.container_get_properties(account, bucket,
                                                        reqid=kwargs['reqid'])
            except NoSuchContainer:
                self.log.info('Bucket %s does not exist', bucket)
                return "Error"

            sys = data['system']
            # WARN it doe not reflect namespace versioning if enabled
            status = sys.get(M2_PROP_VERSIONING_POLICY, None)
            if status is None or int(status) == 0:
                return "Suspended"
            else:
                return "Enabled"

        if parsed_args.versioning:
            columns += ('Versioning', )

            def enrich(listing):
                for v in listing:
                    v['versioning'] = versioning(v['name'])
                    yield v
            listing = enrich(listing)

        return columns, ([v[k.lower()] for k in columns] for v in listing)


class ListContainer(Lister):
    """List containers."""

    log = getLogger(__name__ + '.ListContainer')

    def get_parser(self, prog_name):
        from oio.cli.common.utils import ValueFormatStoreTrueAction

        parser = super(ListContainer, self).get_parser(prog_name)
        parser.add_argument(
            '--prefix',
            metavar='<prefix>',
            help='Filter list using <prefix>'
        )
        parser.add_argument(
            '--delimiter',
            metavar='<delimiter>',
            help='Delimiter'
        )
        parser.add_argument(
            '--marker',
            metavar='<marker>',
            help='Marker for paging'
        )
        parser.add_argument(
            '--end-marker',
            metavar='<end-marker>',
            help='End marker for paging'
        )
        parser.add_argument(
            '--limit',
            metavar='<limit>',
            help='Limit the number of containers returned'
        )
        parser.add_argument(
            '--no-paging', '--full',
            dest='full_listing',
            default=False,
            help=("List all containers without paging "
                  "(and set output format to 'value')"),
            action=ValueFormatStoreTrueAction
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        from oio.common.utils import cid_from_name

        kwargs = {}
        if parsed_args.prefix:
            kwargs['prefix'] = parsed_args.prefix
        if parsed_args.marker:
            kwargs['marker'] = parsed_args.marker
        if parsed_args.end_marker:
            kwargs['end_marker'] = parsed_args.end_marker
        if parsed_args.delimiter:
            kwargs['delimiter'] = parsed_args.delimiter
        if parsed_args.limit:
            kwargs['limit'] = parsed_args.limit

        account = self.app.client_manager.account

        if parsed_args.full_listing:
            def full_list():
                listing = self.app.client_manager.storage.container_list(
                    account, **kwargs)
                for element in listing:
                    yield element

                while listing:
                    kwargs['marker'] = listing[-1][0]
                    listing = self.app.client_manager.storage.container_list(
                        account, **kwargs)
                    if listing:
                        for element in listing:
                            yield element

            listing = full_list()
        else:
            listing = self.app.client_manager.storage.container_list(
                account, **kwargs)

        columns = ('Name', 'Bytes', 'Count', 'Mtime', 'CID')
        return columns, ((v[0], v[2], v[1], v[4], cid_from_name(account, v[0]))
                         for v in listing)


class UnsetContainer(ContainerCommandMixin, Command):
    """Unset container properties."""

    log = getLogger(__name__ + '.UnsetContainer')

    def get_parser(self, prog_name):
        parser = super(UnsetContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        parser.add_argument(
            '--bucket-name',
            action='store_true',
            help=('Declare the container no more belongs to any bucket')
        )
        parser.add_argument(
            '--property',
            metavar='<key>',
            action='append',
            default=[],
            help='Property to remove from container',
        )
        parser.add_argument(
            '--storage-policy', '--stgpol',
            action='store_true',
            help='Reset the storage policy of the container '
                 'to the namespace default'
        )
        parser.add_argument(
            '--max-versions', '--versioning',
            action='store_true',
            help='Reset the versioning policy of the container '
                 'to the namespace default'
        )
        parser.add_argument(
            '--quota',
            action='store_true',
            help='Reset the quota of the container '
                 'to the namespace default'
        )
        parser.add_argument(
            '--delete-exceeding-versions',
            action='store_true',
            help='Reset the deletion of the exceeding versions '
                 'to the default value'
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        self.take_action_container(parsed_args)
        properties = parsed_args.property
        system = dict()
        if parsed_args.bucket_name:
            system[M2_PROP_BUCKET_NAME] = ''
        if parsed_args.storage_policy:
            system[M2_PROP_STORAGE_POLICY] = ''
        if parsed_args.max_versions:
            system[M2_PROP_VERSIONING_POLICY] = ''
        if parsed_args.quota:
            system[M2_PROP_QUOTA] = ''
        if parsed_args.delete_exceeding_versions:
            system[M2_PROP_DEL_EXC_VERSIONS] = ''

        if properties or not system:
            self.app.client_manager.storage.container_del_properties(
                self.app.client_manager.account,
                parsed_args.container,
                properties, cid=parsed_args.cid)
        if system:
            self.app.client_manager.storage.container_set_properties(
                self.app.client_manager.account,
                parsed_args.container,
                system=system, cid=parsed_args.cid)


class SaveContainer(ContainerCommandMixin, Command):
    """Save all objects of a container locally."""

    log = getLogger(__name__ + '.SaveContainer')

    def get_parser(self, prog_name):
        parser = super(SaveContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        return parser

    def take_action(self, parsed_args):
        import os

        self.log.debug('take_action(%s)', parsed_args)
        self.take_action_container(parsed_args)

        account = self.app.client_manager.account
        container = parsed_args.container
        cid = parsed_args.cid
        objs = self.app.client_manager.storage.object_list(
            account, container, cid=cid)

        for obj in objs['objects']:
            obj_name = obj['name']
            _, stream = self.app.client_manager.storage.object_fetch(
                account, container, obj_name, properties=False, cid=cid)

            if not os.path.exists(os.path.dirname(obj_name)):
                if len(os.path.dirname(obj_name)) > 0:
                    os.makedirs(os.path.dirname(obj_name))
            with open(obj_name, 'wb') as f:
                for chunk in stream:
                    f.write(chunk)


class LocateContainer(ContainerCommandMixin, ShowOne):
    """Locate the services in charge of a container."""

    log = getLogger(__name__ + '.LocateContainer')

    def get_parser(self, prog_name):
        parser = super(LocateContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        self.take_action_container(parsed_args)

        account = self.app.client_manager.account
        container = parsed_args.container
        cid = parsed_args.cid
        m2_sys = self.app.client_manager.storage.container_get_properties(
            account, container, cid=cid)['system']

        data_dir = self.app.client_manager.storage.directory.list(
            account, container, cid=cid)

        info = {
            'account': m2_sys['sys.account'],
            'base_name': m2_sys['sys.name'],
            'name': m2_sys['sys.user.name'],
            'meta0': list(),
            'meta1': list(),
            'meta2': list(),
            'meta2.sys.peers': list(),
            'status': OIO_DB_STATUS_NAME.get(m2_sys.get('sys.status'),
                                             "Unknown"),
        }

        for d in data_dir['srv']:
            if d['type'] == 'meta2':
                info['meta2'].append(d['host'])

        for peer in m2_sys.get('sys.peers', 'Unknown').split(','):
            info['meta2.sys.peers'].append(peer)

        for d in data_dir['dir']:
            if d['type'] == 'meta0':
                info['meta0'].append(d['host'])
            if d['type'] == 'meta1':
                info['meta1'].append(d['host'])

        for stype in ["meta0", "meta1", "meta2", 'meta2.sys.peers']:
            info[stype] = ', '.join(info[stype])
        return list(zip(*sorted(info.items())))


class PurgeContainer(ContainerCommandMixin, Command):
    """Purge exceeding object versions."""

    log = getLogger(__name__ + '.PurgeContainer')

    def get_parser(self, prog_name):
        parser = super(PurgeContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        parser.add_argument(
            '--max-versions',
            metavar='<n>',
            type=int,
            help="""The number of versions to keep
 (overrides the container configuration).
 n<0 is unlimited number of versions (purge only deleted aliases).
 n=0 is 1 version.
 n>0 is n versions.
"""
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        self.take_action_container(parsed_args)

        account = self.app.client_manager.account
        self.app.client_manager.storage.container_purge(
            account, parsed_args.container,
            maxvers=parsed_args.max_versions,
            cid=parsed_args.cid
        )


class RefreshBucket(Command):
    """
    Refresh the counters of a bucket.

    Reset all statistics counters and recompute them by summing
    the counters of all shards (containers).
    """

    log = getLogger(__name__ + '.RefreshBucket')

    def get_parser(self, prog_name):
        parser = super(RefreshBucket, self).get_parser(prog_name)
        parser.add_argument(
            'bucket',
            help='Name of the bucket to refresh.'
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        reqid = request_id(prefix='CLI-BUCKET-')
        acct_client = self.app.client_manager.storage.account
        acct_client.bucket_refresh(parsed_args.bucket, reqid=reqid)


class RefreshContainer(ContainerCommandMixin, Command):
    """ Refresh counters of an account (triggers asynchronous treatments) """

    log = getLogger(__name__ + '.RefreshContainer')

    def get_parser(self, prog_name):
        parser = super(RefreshContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        self.take_action_container(parsed_args)
        if parsed_args.cid is None:
            account = self.app.client_manager.account
            container = parsed_args.container
        else:
            account, container = \
                self.app.client_manager.storage.resolve_cid(
                    parsed_args.cid)
        self.app.client_manager.storage.container_refresh(
            account=account, container=container)


class SnapshotContainer(ContainerCommandMixin, Lister):
    """
    Take a snapshot of a container.

    Create a separate database containing all information about the contents
    from the original database, but with copies of the chunks at the time
    of the snapshot. This new database is frozen (you cannot write into it).

    Pay attention to the fact that the source container is frozen during
    the snapshot capture. The capture may take some time, depending on
    the number of objects hosted by the container.
    """

    log = getLogger(__name__ + '.SnapshotContainer')

    def get_parser(self, prog_name):
        parser = super(SnapshotContainer, self).get_parser(prog_name)
        self.patch_parser_container(parser)
        parser.add_argument(
            '--dst-account',
            metavar='<account>',
            help=('The account where the snapshot should be created. '
                  'By default the same account as the snapshotted container.')
        )
        parser.add_argument(
            '--dst-container',
            metavar='<container>',
            help=('The name of the container hosting the snapshot. '
                  'By default the name of the snapshotted container '
                  'suffixed by a timestamp.')
        )
        parser.add_argument(
            '--chunk-batch-size',
            metavar='<size>',
            default=100,
            help=('The number of chunks updated at the same time.')
        )
        parser.add_argument(
            '--timeout',
            default=60.0,
            type=float,
            help=('Timeout for the operation (default: 60.0s).')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)
        self.take_action_container(parsed_args)
        cid = parsed_args.cid
        if cid is None:
            account = self.app.client_manager.account
            container = parsed_args.container
        else:
            account, container = \
                self.app.client_manager.storage.resolve_cid(cid)
        deadline = timeout_to_deadline(parsed_args.timeout)
        dst_account = parsed_args.dst_account or account
        dst_container = (parsed_args.dst_container or
                         (container + "-" + Timestamp().normal))
        batch_size = parsed_args.chunk_batch_size

        self.app.client_manager.storage.container_snapshot(
            account, container, dst_account,
            dst_container, batch_size=batch_size,
            deadline=deadline)
        lines = [(dst_account, dst_container, "OK")]
        return ('Account', 'Container', 'Status'), lines
