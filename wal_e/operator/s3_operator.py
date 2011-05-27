import argparse
import contextlib
import csv
import datetime
import errno
import glob
import json
import logging
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap

import wal_e.worker.s3_worker as s3_worker
import wal_e.tar_partition as tar_partition
import wal_e.log_help as log_help

from wal_e.exception import UserException, UserCritical
from wal_e.piper import popen_sp
from wal_e.storage import s3_storage
from wal_e.worker.psql_worker import PSQL_BIN, PgBackupStatements
from wal_e.worker.s3_worker import LZOP_BIN, S3CMD_BIN, MBUFFER_BIN
from wal_e.worker.s3_worker import check_call_wait_sigint

logger = log_help.get_logger(__name__)

# Provides guidence in object names as to the version of the file
# structure.
FILE_STRUCTURE_VERSION = s3_storage.StorageLayout.VERSION

class S3Backup(object):
    """
    A performs s3cmd uploads to of PostgreSQL WAL files and clusters

    """

    def __init__(self,
                 aws_access_key_id, aws_secret_access_key, s3_prefix):
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

        # Canonicalize the s3 prefix by stripping any trailing slash
        self.s3_prefix = s3_prefix.rstrip('/')

    @property
    @contextlib.contextmanager
    def s3cmd_temp_config(self):
        with tempfile.NamedTemporaryFile(mode='w') as s3cmd_config:
            s3cmd_config.write(textwrap.dedent("""\
            [default]
            access_key = {aws_access_key_id}
            secret_key = {aws_secret_access_key}
            use_https = True
            """).format(aws_access_key_id=self.aws_access_key_id,
                        aws_secret_access_key=self.aws_secret_access_key))

            s3cmd_config.flush()
            s3cmd_config.seek(0)

            yield s3cmd_config

    def backup_list(self, query, detail, detail_retry, detail_timeout,
                    list_retry, list_timeout):
        """
        Lists base backups and basic information about them

        """
        import csv

        from boto.s3.connection import OrdinaryCallingFormat
        from boto.s3.connection import S3Connection

        from wal_e.storage.s3_storage import BackupInfo

        s3_conn = S3Connection(self.aws_access_key_id,
                               self.aws_secret_access_key,
                               calling_format=OrdinaryCallingFormat())

        bl = s3_worker.BackupList(s3_conn,
                                  s3_storage.StorageLayout(self.s3_prefix),
                                  detail, detail_retry, detail_timeout,
                                  list_retry, list_timeout)

        # If there is no query, return an exhaustive list, otherwise
        # find a backup instad.
        if query is None:
            bl_iter = bl
        else:
            bl_iter = bl.find_all(query)

        # TODO: support switchable formats for difference needs.
        w_csv = csv.writer(sys.stdout, dialect='excel-tab')
        w_csv.writerow(BackupInfo._fields)

        for backup_info in bl_iter:
            w_csv.writerow(backup_info)

        sys.stdout.flush()

    def _s3_upload_pg_cluster_dir(self, start_backup_info, pg_cluster_dir,
                                  version, pool_size, rate_limit=None):
        """
        Upload to s3_url_prefix from pg_cluster_dir

        This function ignores the directory pg_xlog, which contains WAL
        files and are not generally part of a base backup.

        Note that this is also lzo compresses the files: thus, the number
        of pooled processes involves doing a full sequential scan of the
        uncompressed Postgres heap file that is pipelined into lzo. Once
        lzo is completely finished (necessary to have access to the file
        size) the file is sent to S3.

        TODO: Investigate an optimization to decouple the compression and
        upload steps to make sure that the most efficient possible use of
        pipelining of network and disk resources occurs.  Right now it
        possible to bounce back and forth between bottlenecking on reading
        from the database block device and subsequently the S3 sending
        steps should the processes be at the same stage of the upload
        pipeline: this can have a very negative impact on being able to
        make full use of system resources.

        Furthermore, it desirable to overflowing the page cache: having
        separate tunables for number of simultanious compression jobs
        (which occupy /tmp space and page cache) and number of uploads
        (which affect upload throughput) would help.

        """

        # Get a manifest of files first.
        matches = []

        def raise_walk_error(e):
            raise e

        walker = os.walk(pg_cluster_dir, onerror=raise_walk_error)
        for root, dirnames, filenames in walker:
            is_cluster_toplevel = (os.path.abspath(root) ==
                                   os.path.abspath(pg_cluster_dir))

            # Do not capture any WAL files, although we do want to
            # capture the WAL directory or symlink
            if is_cluster_toplevel:
                if 'pg_xlog' in dirnames:
                    dirnames.remove('pg_xlog')
                    matches.append(os.path.join(root, 'pg_xlog'))

            for filename in filenames:
                if is_cluster_toplevel and filename in ('postmaster.pid',
                                                        'postgresql.conf'):
                    # Do not include the postmaster pid file or the
                    # configuration file in the backup.
                    pass
                else:
                    matches.append(os.path.join(root, filename))

            # Special case for empty directories
            if not filenames:
                matches.append(root)


        backup_s3_prefix = ('{0}/basebackups_{1}/'
                               'base_{file_name}_{file_offset}'
                               .format(self.s3_prefix, FILE_STRUCTURE_VERSION,
                                       **start_backup_info))

        # absolute upload paths are used for telling lzop what to compress
        local_abspaths = [os.path.abspath(match) for match in matches]

        # computed to subtract out extra extraneous absolute path
        # information when storing on S3
        common_local_prefix = os.path.commonprefix(local_abspaths)

        partitions = tar_partition.tar_partitions_plan(
            common_local_prefix, local_abspaths,

            # 1610612736 bytes == 1.5 gigabytes, per partition,
            # non-tunable
            1610612736)

        # A multiprocessing pool to do the uploads with
        pool = multiprocessing.Pool(processes=pool_size)

        if rate_limit is None:
            per_process_limit = None
        else:
            per_process_limit = int(rate_limit / pool_size)

        # Reject tiny per-process rate limits.  They should be
        # rejected more nicely elsewher.
        assert per_process_limit > 0 or per_process_limit is None

        # a list to accumulate async upload jobs
        uploads = []

        total_size = 0
        with self.s3cmd_temp_config as s3cmd_config:

            # Make an attempt to upload extended version metadata
            with tempfile.NamedTemporaryFile(mode='w') as version_tempf:
                version_tempf.write(unicode(version))
                version_tempf.flush()

                check_call_wait_sigint(
                    [S3CMD_BIN, '-c', s3cmd_config.name,
                     '--mime-type=text/plain', 'put',
                     version_tempf.name,
                     backup_s3_prefix + '/extended_version.txt'])

            # Enqueue uploads for parallel execution
            try:
                for tpart_number, tpart in enumerate(partitions):
                    total_size += tpart.total_member_size
                    uploads.append(pool.apply_async(
                            s3_worker.do_partition_put,
                            [backup_s3_prefix, tpart_number, tpart,
                             per_process_limit, s3cmd_config.name]))

                pool.close()
            finally:
                # Necessary in case finally block gets hit before
                # .close()
                pool.close()

                while uploads:
                    # XXX: Need timeout to work around Python bug:
                    #
                    # http://bugs.python.org/issue8296
                    uploads.pop().get(1e100)

                pool.join()

        return backup_s3_prefix, total_size

    def database_s3_fetch(self, pg_cluster_dir, backup_name, pool_size):
        basebackups_prefix = '/'.join(
            [self.s3_prefix, 'basebackups_' + FILE_STRUCTURE_VERSION])

        with self.s3cmd_temp_config as s3cmd_config:

            # Verify sane looking input for backup_name
            if backup_name == 'LATEST':
                # "LATEST" is a special backup name that is always valid
                # to always find the lexically-largest backup, with the
                # intend of getting the freshest database as soon as
                # possible.

                backup_find = popen_sp(
                    [S3CMD_BIN, '-c', s3cmd_config.name,
                     'ls', basebackups_prefix + '/'],
                    stdout=subprocess.PIPE)
                stdout, stderr = backup_find.communicate()


                sentinel_suffix = '_backup_stop_sentinel.json'
                # Find sentinel files as markers of guaranteed good backups
                sentinel_urls = []

                for line in (l.strip() for l in stdout.split('\n')):

                    if line.endswith(sentinel_suffix):
                        sentinel_urls.append(line.split()[-1])

                if not sentinel_urls:
                    raise UserException(
                        'no base backups found',
                        'The prefix searched was "{0}"'
                        .format(basebackups_prefix),
                        'Consider checking to make sure that you have taken a '
                        'base backup and that the prefix is correct.  '
                        'New versions of WAL-E with new file formats will '
                        'fail to recognize old backups, too.')
                else:
                    sentinel_urls.sort()

                    # Slice away the extra URL cruft to locate just
                    # the base backup name.
                    #
                    # NB: '... + 1' is for trailing slash
                    begin_slice = len(basebackups_prefix) + 1
                    end_slice = -len(sentinel_suffix)
                    backup_name = sentinel_urls[-1][begin_slice:end_slice]

            base_backup_regexp = (r'base'
                                  r'_(?P<segment>[0-9a-zA-Z.]{0,60})'
                                  r'_(?P<position>[0-9A-F]{8})')
            match = re.match(base_backup_regexp, backup_name)
            if match is None:
                raise UserException('non-conformant backup name passed',
                                    'The invalid name was "{0}"'
                                    .format(backup_name))

            assert backup_name != 'LATEST', ('Must be rewritten to the actual '
                                             'name of the last base backup')


            backup_s3_prefix = '/'.join([basebackups_prefix, backup_name])
            backup_s3_cluster_prefix = '/'.join(
                [backup_s3_prefix, 'tar_partitions', ''])

            ls_proc = popen_sp(
                [S3CMD_BIN, '-c', s3cmd_config.name, 'ls',
                 backup_s3_cluster_prefix],
                stdout=subprocess.PIPE)
            stdout, stderr = ls_proc.communicate()

            pool = multiprocessing.Pool(processes=pool_size)
            results = []
            cluster_abspath = os.path.abspath(pg_cluster_dir)
            try:
                partitions = set()

                for line in stdout.split('\n'):
                    # Skip any blank lines
                    if not line.strip():
                        continue

                    pos = line.rfind('s3://')
                    if pos > 0:
                        s3_url = line[pos:]
                        assert s3_url.startswith(backup_s3_cluster_prefix)
                        base, partition_name = s3_url.rsplit('/', 1)
                        match = re.match(r'part_(\d+).tar.lzo', partition_name)
                        if not match:
                            raise UserCritical(
                                'Malformed tar partition in base backup',
                                'The offensive line from s3cmd is ' + line)
                        else:
                            partition_number = int(match.group(1))
                            assert partition_number not in partitions, \
                                'Must not have duplicates'
                            partitions.add(partition_number)
                    else:
                        raise UserCritical(
                            'could not parse s3cmd output',
                            'Could not find "s3://" in {0}'.format(line))

                # Verify that partitions look sane: they should be
                # numbered contiguously from 0 to some number.
                expected_partitions = set(xrange(max(partitions) + 1))
                if partitions != expected_partitions:
                    raise UserCritical(
                        'some tar partitions are missing in the base backup',
                        'The following is a list of missing partition '
                        'numbers: ' +
                        unicode(expected_partitions - partitions),
                        'If this is a backup with a sentinel, S3 may be '
                        'inconsistent -- try again later, and note how long '
                        'it takes.  If not, there is a bug somewhere.')
                else:
                    assert expected_partitions == partitions

                    for tpart_number in partitions:
                        results.append(pool.apply_async(
                                s3_worker.do_partition_get,
                                [backup_s3_prefix, cluster_abspath,
                                 tpart_number, s3cmd_config.name]))

                pool.close()
            finally:
                # Necessary in case finally block gets hit before
                # .close()
                pool.close()

                while results:
                    # XXX: Need timeout to work around Python bug:
                    #
                    # http://bugs.python.org/issue8296
                    results.pop().get(1e100)

                pool.join()


    def database_s3_backup(self, *args, **kwargs):
        """
        Uploads a PostgreSQL file cluster to S3

        Mechanism: just wraps _s3_upload_pg_cluster_dir with
        start/stop backup actions with exception handling.

        In particular there is a 'finally' block to stop the backup in
        most situations.

        """

        upload_good = False
        backup_stop_good = False
        try:
            start_backup_info = PgBackupStatements.run_start_backup()
            version = PgBackupStatements.pg_version()['version']
            uploaded_to, expanded_size_bytes = \
                self._s3_upload_pg_cluster_dir(
                start_backup_info, version=version,
                *args, **kwargs)
            upload_good = True
        finally:
            if not upload_good:
                logger.warning(
                    log_help.fmt_logline(
                        'blocking on sending WAL segments',
                        'The backup was not completed successfully, '
                        'but we have to wait anyway.  '
                        'See README: TODO about pg_cancel_backup'))

            stop_backup_info = PgBackupStatements.run_stop_backup()
            backup_stop_good = True

        if upload_good and backup_stop_good:
            # Make a best-effort attempt to write a sentinel file to
            # the cluster backup directory that indicates that the
            # base backup upload has definitely run its course (it may
            # have, even without this file, though) and also
            # communicates what WAL segments are needed to get to
            # consistency.
            try:
                with self.s3cmd_temp_config as s3cmd_config:
                    with tempfile.NamedTemporaryFile(mode='w') as sentinel:
                        json.dump(
                            {'wal_segment_backup_stop':
                                 stop_backup_info['file_name'],
                             'wal_segment_offset_backup_stop':
                                 stop_backup_info['file_offset'],
                             'expanded_size_bytes': expanded_size_bytes},
                            sentinel)
                        sentinel.flush()

                        # Avoid using do_lzop_s3_put to store
                        # uncompressed: easier to read/double click
                        # on/dump to terminal
                        check_call_wait_sigint(
                            [S3CMD_BIN, '-c', s3cmd_config.name,
                             '--mime-type=application/json', 'put',
                             sentinel.name,
                             uploaded_to + '_backup_stop_sentinel.json'])
            except KeyboardInterrupt, e:
                # Specially re-raise exception on SIGINT to allow
                # propagation.
                raise
            except:
                # Failing to upload the sentinel is not strictly
                # lethal, so ignore any (other) exception.
                pass
        else:
            # NB: Other exceptions should be raised before this that
            # have more informative results, it is intended that this
            # exception never will get raised.
            raise UserCritical('could not complete backup process')

    def wal_s3_archive(self, wal_path):
        """
        Uploads a WAL file to S3

        This code is intended to typically be called from Postgres's
        archive_command feature.

        """
        wal_file_name = os.path.basename(wal_path)

        with self.s3cmd_temp_config as s3cmd_config:
            s3_worker.do_lzop_s3_put(
                '{0}/wal_{1}/{2}'.format(self.s3_prefix,
                                         FILE_STRUCTURE_VERSION,
                                         wal_file_name),
                wal_path, s3cmd_config.name)

    def wal_s3_restore(self, wal_name, wal_destination):
        """
        Downloads a WAL file from S3

        This code is intended to typically be called from Postgres's
        restore_command feature.

        NB: Postgres doesn't guarantee that wal_name ==
        basename(wal_path), so both are required.

        """
        with self.s3cmd_temp_config as s3cmd_config:
            s3_worker.do_lzop_s3_get(
                '{0}/wal_{1}/{2}.lzo'.format(self.s3_prefix,
                                             FILE_STRUCTURE_VERSION,
                                             wal_name),
                wal_destination, s3cmd_config.name)

    def wal_fark(self, pg_cluster_dir):
        """
        A function that performs similarly to the PostgreSQL archiver

        It's the FAke ArKiver.

        This is for use when the database is not online (and can't
        archive its segments) or in put into standby mode to quiesce
        the system.  It was written to be useful in switchover
        scenarios.

        Notes:

        It is questionable if it belongs to class S3Backup (where it
        began life)

        It'd be nice if there was a way to use the PostgreSQL archiver
        separately from the database, possibly with enough hooks to
        enable parallel archiving.

        """
        xlog_dir = os.path.join(pg_cluster_dir, 'pg_xlog')
        archive_status_dir = os.path.join(xlog_dir, 'archive_status')
        suffix = '.ready'
        for ready_path in sorted(glob.iglob(os.path.join(
                    archive_status_dir, '*.ready'))):
            try:
                assert ready_path.endswith(suffix)
                archivee_name = os.path.basename(ready_path)[:-len(suffix)]
                self.wal_s3_archive(os.path.join(xlog_dir, archivee_name))
            except OSError, e:
                if e.errno == errno.ENOENT:
                    raise UserException(
                        'wal_fark could not read a file'
                        'The file that could not be read is at:' +
                        unicode(e.filename))
                else:
                    raise

            # It's critically important that this only happen if
            # the archiving completed successfully, but an ENOENT
            # can happen if there is a (harmless) race condition,
            # assuming that anyone who would move the archive
            # status file has done the requisite work.
            try:
                done_path = os.path.join(os.path.dirname(ready_path),
                                         archivee_name + '.done')
                os.rename(ready_path, done_path)
            except OSError, e:
                if e.errno == errno.ENOENT:
                    print >>sys.stderr, \
                        ('Archive status file {ready_path} no longer '
                         'exists, there may be another racing archiving '
                         'process.  This is harmless IF ALL ARCHIVERS ARE '
                         'WELL-BEHAVED but potentially wasteful.'
                         .format(ready_path=ready_path))
                else:
                    raise

