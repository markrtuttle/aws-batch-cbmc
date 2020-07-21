#!/usr/bin/env python3

# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import argparse
import datetime
import json
import logging
import os
import re
import tempfile
import itertools
import sys
import functools
import requests

import botocore_amazon.monkeypatch
import boto3
import botocore

################################################################
# print for stderr:


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

################################################################

def create_parser():
    arg = argparse.ArgumentParser(
        description='Scan AWS logs to debug validation errors.')

    arg.add_argument('--profile',
                     metavar='PROFILE',
                     required=True,
                     help="""
                     The profile for the AWS account with validation errors.
                     """
                    )
    arg.add_argument('--utc',
                     metavar='UTC',
                     required=True,
                     help="""
                     The approximate time of the error being debugged.
                     The logs are search for an interval of time including
                     this time.
                     This is UTC time given by a valid ISO date string
                     such as YYYY-MM-DDTHH:MM:SS.
                     """
                    )

    # the default interval is 70 minutes back, 20 minutes forward.
    # Most of the time, we are using this tool to diagnose proof failures with a timestamp
    # associated with the failure.  Currently, proofs run in 60 minutes or less,
    # so the backwards interval (70) should be sufficient to capture the CI runs that
    # contain the error.  For the 20 minutes ahead, sometimes the errors do not
    # terminate the CI run; to get the full picture, we allow a 20 minute lookahead.
    default_interval = [70, 20]

    arg.add_argument('--interval',
                     nargs='+',
                     metavar='M',
                     type=int,
                     default=default_interval,
                     help="""
                     The interval of time about UTC used to search the
                     logs.  Use 
                     --interval A B to begin the search A minutes
                     before UTC and end B minutes after UTC
                     (default: --interval 70 20).
                     """
                    )

    arg.add_argument('--correlation_id',
                     help="""
                     Correlation id used to build task trees and look up proofs.
                     Prints out the failing and incomplete tasks associated
                     with the correlation id.""")


    arg.add_argument('--task_tree',
                     action="store_true",
                     help="""
                     Prints out the task tree for a proof (requires --correlation_id).
                     """
                    )

    arg.add_argument('--diagnose',
                     action="store_true",
                     help="""
                     Attempts to localize failure within a proof (if no --correlation_id,
                     runs on all executions within the interval). 
                     """
                    )

    arg.add_argument('--max_log_entries',
                     action='store',
                     type=int,
                     default=20,
                     help="""
                     Maximum log entries to display when diagnosing failure (default: 20).
                     """
                     )

    arg.add_argument('--proofs',
                     nargs="+",
                     help="""
                     A list of proof identifiers to look up in the logs.
                     """
                    )

    arg.add_argument('--detail',
                     type=int,
                     default=1,
                     help="""
                     Level of detail to print for information.
                     """
                    )

    arg.add_argument('--verbose',
                     action='store_true',
                     help='Verbose output.'
                    )

    arg.add_argument('--debug',
                     action='store_true',
                     help='Debug output.'
                    )

    arg.add_argument('--pprint',
                     action='store_true',
                     help="""
                        If set, the json output is pretty-printed (good for human-reading),
                        otherwise it is printed on one line.
                        """)


    return arg

################################################################

def time_from_iso(timeiso):
    if timeiso is None:
        return None
    lcltime = datetime.datetime.fromisoformat(timeiso)
    gmttime = lcltime.replace(tzinfo=datetime.timezone.utc)
    return int(gmttime.timestamp() * 1000)

def iso_from_time(timems):
    if timems is None:
        return None
    return datetime.datetime.utcfromtimestamp(timems // 1000).isoformat()

def timestamp_interval(utc, interval):
    if utc == 'now':
        utctime = int(datetime.datetime.utcnow().timestamp() * 1000)
    else:
        utctime = time_from_iso(utc)

    start = interval[0]
    try:
        end = interval[1]
    except IndexError:
        end = None
    utcstart = utctime - (start * 60 * 1000)
    utcend = utctime + (end * 60 * 1000) if end else None
    return utcstart, utcend

################################################################

class LogGroups:
    """Manage the log groups for AWS CloudWatch logs."""

    def __init__(self, session):
        self.client = session.client('logs')
        # Updating a cloudwatch stack can create a second instance
        # of a log group with a different hexadecimal suffix.
        # We restrict attention to the most recently created log group.
        self.log_groups = sorted(
            self.client.describe_log_groups()['logGroups'],
            key=lambda group: group['creationTime'],
            reverse=True
        )

    def log_group(self, name):
        """Log group whose name contains name as a substring."""

        log_groups = [group['logGroupName'] for group in self.log_groups
                      if name.lower() in group['logGroupName'].lower()]
        if not log_groups:
            logging.info("Failed to find log group with name %s", name)
            return None
        if len(log_groups) > 1:
            logging.info("Ignoring log groups with name %s: %s",
                         name, log_groups)
        log_name = log_groups[0]
        logging.info("Found log group with name %s: %s", name, log_name)
        return log_name

    def webhook(self):
        """Log group for the webhook lambda."""
        return self.log_group('github-HandleWebhookLambda')

    # Here until we no longer are interested in logs for 'old' proofs.
    def invoke(self):
        """Log group for the batch invocation lambda."""
        return self.log_group('github-InvokeBatchLambda')

    def status(self):
        """Log group for the batch status lambda."""
        return self.log_group('github-BatchStatusLambda')

    def batch(self):
        """Log group for AWS Batch."""
        return self.log_group('/aws/batch/job')

    def prepare(self):
        """Log group for prepare source."""
        return self.log_group('prepare')

    def matching_streams(self, log_group, pattern=None,
                         start_timestamp=None, end_timestamp=None,
                         text=None, log_stream_names=None):
        log_group = self.log_group(log_group)

        kwargs = {"logGroupName": log_group,
                  "startTime": start_timestamp,
                  "endTime": end_timestamp,
                  "filterPattern": pattern}
        if log_stream_names:
            kwargs['logStreamNames'] = log_stream_names

        paginator = self.client.get_paginator('filter_log_events')
        page_iterator = paginator.paginate(**kwargs)
        logging.info("LogGroups filter log events arguments: %s", kwargs)

        log_items = []
        text = text or 'Reading logs'

        logging.info(text)
        for page in page_iterator:
            log_items.extend(page['events'])
        logging.info(" done (found {} items)".format(len(log_items)))
        logging.debug("LogGroups filter log events results: %s", log_items)

        log_streams = list({event['logStreamName'] for event in log_items})
        logging.info("Returning log group: %s", log_group)
        logging.info("Returning log streams: %s", log_streams)
        logging.debug("Returning log items: %s", log_items)
        return log_group, log_streams, log_items

    def read_stream(self, log_group, log_stream):
        kwargs = {'logGroupName': log_group,
                  'logStreamName': log_stream,
                  'startFromHead': True
                  }
        events = self.client.get_log_events(**kwargs)
        return events

    def read_stream_last(self, log_group, log_stream, number):
        kwargs = {'logGroupName': log_group,
                  'logStreamName': log_stream,
                  'startFromHead': False,
                  'limit': number
                  }
        events = self.client.get_log_events(**kwargs)
        return events


################################################################
# PrepareLog should be called as part of the task tree failure information.

class PrepareLog:
    """Manage the AWS CodeBuild log for Prepare-Source invoking CBMC."""

    def __init__(self, log_groups, log_group, log_stream):

        self.log_group = log_group
        self.log_stream = log_stream

        log_json = log_groups.read_stream(self.log_group, self.log_stream)
        self.repository = prepare_repository(log_json)
        self.commit = prepare_commit(log_json)
        self.tarfile = prepare_tarfile(log_json)
        self.proofs = prepare_proofs(log_json)

    def summary(self, detail=1):
        result = {
            'repository': self.repository,
            'commit': self.commit,
            'tarfile': self.tarfile
        }
        if detail > 1:
            result['log_group'] = self.log_group
            result['log_stream'] = self.log_stream
            result['proofs'] = self.proofs
        return result
#        return {'CBMCInvocation': result}

def prepare_repository(log_json):
    # Log message format is
    # INFO: Running "git clone REPO" in "DIR"
    messages = [event['message'].strip() for event in log_json['events']
                if event['message'].startswith('INFO: Running "git clone')]
    assert len(messages) == 1
    match = re.search('"git clone (.+) .+" in ".+"', messages[0])
    repo = match.group(1)
    logging.info('Found repository in prepare log:  %s', repo)
    return repo

def prepare_commit(log_json):
    # Log message format is
    # INFO: Running "git checkout COMMIT" in "DIR"
    merge_messages = [event['message'].strip() for event in log_json['events']
                if event['message'].startswith('INFO: Running "git merge')]
    checkout_messages = [event['message'].strip() for event in log_json['events']
                if event['message'].startswith('INFO: Running "git checkout')]
    assert len(merge_messages) + len(checkout_messages) == 1

    if checkout_messages:
        match = re.search('"git checkout (.+)" in ".+"', checkout_messages[0])
        commit = match.group(1)
    elif merge_messages:
        match = re.search('"git merge --no-edit (.+)" in ".+"', merge_messages[0])
        commit = match.group(1)
    logging.info('Found commit in prepare log:  %s', commit)
    return commit

def prepare_tarfile(log_json):
    # Log message format is
    # INFO: Running "tar fcz TARFILE DIR" in "DIR"
    messages = [event['message'].strip() for event in log_json['events']
                if event['message'].startswith('INFO: Running "tar')]
    assert len(messages) == 1
    tarfile = messages[0].split()[4]
    logging.info('Found tarfile in prepare log: %s', tarfile)
    return tarfile

def prepare_proofs(log_json):
    # Log message format is
    # Launching job PROOF:
    messages = [event['message'].strip() for event in log_json['events']
                if event['message'].startswith('Launching job')]
    matches = [re.search('Launching job (.*):', msg) for msg in messages]
    assert all(matches)
    proofs = [match.group(1) for match in matches]
    logging.info("Found proofs in prepare log: %s", proofs)
    return proofs

class PrepareLogs:
    def __init__(self, log_groups, proofs=None, start=None, end=None):
        pattern = ""
        if isinstance(proofs, str):
            pattern = '"{}"'.format(proofs)
        if isinstance(proofs, list):
            pattern = ' '.join(['?"{}"'.format(proof) for proof in proofs])


        log_group, log_streams, _ = log_groups.matching_streams(
            log_groups.prepare(), pattern, start, end,
            'Scanning CBMC invocation logs')

        self.prepare_logs = [PrepareLog(log_groups, log_group, log_stream) for log_stream in log_streams]
        self.commits = [log.commit for log in self.prepare_logs]

    def summary(self, detail=1):
        return {'CBMCInvocations': [item.summary(detail) for item in self.prepare_logs]}


################################################################

class InvokeLog:
    """Manage the logs for the Batch Invocation lambda function"""

    def __init__(self, log_groups, log_group, log_stream, commit_list):
        self.log_group = log_group
        self.log_stream = log_stream

        # Webhook payload is the json blog in the log line following
        # "GitHub event:"
        log_json = log_groups.read_stream(self.log_group, self.log_stream)
        messages = [event['message'] for event in log_json['events']]
        webhooks = [i+1 for i in range(len(messages)-1)
                    if messages[i].startswith('GitHub event:')]
        payloads = [{"ts": iso_from_time(log_json['events'][i]['timestamp']), "message": messages[i]} for i in webhooks
                    if (any([commit in messages[i] for commit in commit_list]) if commit_list else True)]
        assert len(payloads) >= 1
        self.webhooks = generate_webhooks(payloads)


    def summary(self, detail=1):
        result = []
        for webhook in self.webhooks:
            elem = {'webhook': webhook.summary()}
            if detail > 1:
                elem['log_group'] = self.log_group
                elem['log_stream'] = self.log_stream
            result.append(elem)
        return result

class InvokeLogs:
    """Manage the logs for the Batch Invocation lambda function"""

    def __init__(self, log_groups, commits=None, start=None, end=None):
        commit_list = []
        pattern = ""
        if isinstance(commits, str):
            pattern = '"{}"'.format(commits)
            commit_list = [commits]
        if isinstance(commits, list):
            pattern = ' '.join(['?"{}"'.format(commit) for commit in commits])
            commit_list = commits
        log_group, log_streams, _ = log_groups.matching_streams(
            log_groups.invoke(), pattern, start, end,
            'Scanning CI invocation logs')

        self.log_group = log_group
        self.invoke_logs = [InvokeLog(log_groups, log_group, log_stream, commit_list) for log_stream in log_streams]

    def summary(self, detail=1):
        invocations = list(itertools.chain(*[item.summary(detail) for item in self.invoke_logs]))
        invocations.sort(key=lambda elem: elem['webhook']['timestamp'])
        return {'ProofInvocations': invocations}

################################################################

def parse_json_fail(field_name, ts):
    logging.error(
        f"Unable to parse {field_name} at time {ts}; most likely it is too large to fit in a cloudwatch event")


def generate_webhooks(payloads):
    webhooks = []
    for payload in payloads:
        try:
            webhook = WebHook(payload['message'], payload['ts'])
            webhooks.append(webhook)
        except:
            parse_json_fail('webhook', payload['ts'])
    return webhooks

class WebHook:
    """Parse the webhook payload."""

    def __init__(self, payload, timestamp=None):

        if isinstance(payload, str):
            # Note: may throw exception if payload is mal-formed
            # as can happen when a message gets truncated.
            webhook = json.loads(payload)
        else:
            webhook = payload

        headers = {k.lower(): v for k, v in webhook["headers"].items()}
        body = json.loads(webhook["body"])

        self.event_type = headers.get('x-github-event')
        # The repository being written to
        self.base_name = None
        self.base_branch = None
        self.base_sha = None
        # The repository being read from
        self.head_name = None
        self.head_branch = None
        self.head_sha = None
        # A url describing the event
        self.url = None
        self.timestamp = timestamp

        if self.event_type == 'pull_request':
            self.base_name = body["pull_request"]["base"]["repo"]["full_name"]
            self.base_branch = body["pull_request"]["base"]["ref"]
            self.base_sha = body["pull_request"]["base"]["sha"]
            self.head_name = body["pull_request"]["head"]["repo"]["full_name"]
            self.head_branch = body["pull_request"]["head"]["ref"]
            self.head_sha = body["pull_request"]["head"]["sha"]
            self.url = body["pull_request"]["html_url"]
            return

        if self.event_type == 'push':
            self.base_name = body["repository"]["full_name"]
            self.base_branch = body["ref"]
            head_commit = None
            if body.get('head_commit'):
                head_commit = body['head_commit']
            elif body.get('commits'):
                head_commit = body['commits'][-1]
            elif body.get('after'):
                head_commit = {'id': body['after']}
            if head_commit:
                self.head_sha = head_commit.get('id')
                self.url = head_commit.get('url')
            return

        raise UserWarning('Unknown event type: {}'.format(self.event_type))

    def summary(self, detail=1):
        result = {
            'timestamp': self.timestamp,
            'event_type': self.event_type,
            'base_name': self.base_name,
            'base_branch': self.base_branch,
            'base_sha': self.base_sha,
            'head_name': self.head_name,
            'head_branch': self.head_branch,
            'head_sha': self.head_sha,
            'url': self.url
        }
        return result


################################################################

class StatusLog:
    """Manage the logs for the Batch Status lambda function."""

    def __init__(self, log_groups, start, end=None):
        self.errors = []
        self.log_group, self.log_streams, events = log_groups.matching_streams(
            log_groups.status(), '"Unexpected Verification Result"',
            start, end, 'Scanning proof status logs')
        for event in events:
            self.errors.append(event['message'].strip().split()[-1])

    def summary(self, detail=1):
        result = self.errors
        if detail > 1:
            result = {'log_group': self.log_group,
                      'log_streams': self.log_streams,
                      'errors': self.errors}
        return {'FailedProofs': result}

################################################################

PROOF_STEP_NAMES = ['build', 'property', 'coverage', 'report']

class ProofStepBatchLog:
    def __init__(self, log_groups, proof_step, log_group, log_stream):
        self.proof_step = proof_step
        self.log_group = log_group
        self.log_stream = log_stream
        self.json = log_groups.read_stream(log_group, log_stream)
        self.text = [event['message'] for event in self.json['events']]

    def summary(self, detail=1):
        result = {
            'text': self.text
        }
        if detail > 2:
            result['log_group'] = self.log_group
            result['log_stream'] = self.log_stream
            result['proof_step'] = self.proof_step
        if detail > 4:
            result['json'] = self.json
        return result


class ProofBatchLog:
    def __init__(self, log_groups, start, end, proof):

        make_proof_step = lambda step: proof + '-' + step
        make_step = lambda proof_step: proof_step.split('-')[-1]
        self.proof = proof
        self.proof_steps = [make_proof_step(step) for step in PROOF_STEP_NAMES]

        pattern = ' '.join('?"{}"'.format(step) for step in self.proof_steps)
        self.log_group, _, log_events = log_groups.matching_streams(
            log_groups.batch(), pattern, start, end,
            'Scanning batch logs for {}'.format(proof))

        self.log_stream = {}
        for proof_step in self.proof_steps:
            for event in log_events:
                if proof_step in event['message']:
                    self.log_stream[make_step(proof_step)] = event['logStreamName']
                    break

        self.log = {}
        for step, log_stream in self.log_stream.items():
            self.log[step] = ProofStepBatchLog(
                log_groups, make_proof_step(step),
                self.log_group, log_stream)

    def summary(self, detail=1):
        result = {}
        for step in self.log_stream:
            result[self.log[step].proof_step] = self.log[step].summary(detail)
        return {'BatchLogs': {self.proof: result}}

################################################################

class ProofBatchStatus:
    def __init__(self, session, start, end):
        client = session.client('batch')

        status_list = ['SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING',
                       'RUNNING', 'SUCCEEDED', 'FAILED']

        paginator = client.get_paginator('list_jobs')
        kwargs = {'jobQueue': 'CBMCJobQueue'}

        self.jobs = {}

        logging.info('Scanning batch status logs')
        for status in status_list:
            kwargs['jobStatus'] = status
            page_iterator = paginator.paginate(**kwargs)
            for page in page_iterator:
                # print(' .', end='', flush=True)
                for job in page['jobSummaryList']:
                    name = job['jobName']
                    created = job['createdAt']
                    if start <= created <= end:
                        self.jobs[name] = job
        logging.info(' done (found {} items)'.format(len(self.jobs)))

    def failures(self):
        result = {}
        for name, job in self.jobs:
            if job['status'] == 'FAILED':
                result[name] = job_summary(job)
        return result

    def report(self, proofs):
        result = {}
        for proof in proofs:
            result[proof] = {}
            for step in PROOF_STEP_NAMES:
                proof_step = '{}-{}'.format(proof, step)
                result[proof][step] = job_summary(self.jobs.get(proof_step))
        return result

    def summary(self, detail):
        result = {}
        for name, job in self.jobs.items():
            if job['status'] == 'FAILED':
                step = name.split('-')[-1]
                proof = name[:-len(step)-1]
                if result.get(proof) is None:
                    result[proof] = {}
                result[proof][step] = job_summary(job)
        return {'FailedContainers': result}

def job_summary(job):
    if job is None:
        return None
    return {'status': job['status'],
            'reason':
                (job['container'].get('reason') if 'container' in job else None) or job['statusReason']}

################################################################

class ProofResult:
    def __init__(self, session, proof):
        self.proof = proof
        client = session.client('s3')

        self.bucket = proof_bucket(client)
        logging.info('Scanning CBMC proof logs for {} .'.format(proof))
        with tempfile.TemporaryDirectory() as tmpdir:
            read_file = lambda name: cbmc_file(client, self.bucket,
                                               proof, name, tmpdir)
            self.log = {
                'build': read_file('build.txt'),
                'property': read_file('cbmc.txt'),
                'coverage': read_file('coverage.xml'),
                'report': read_file('report.txt')
            }
            self.correlation_id = correlation_id_file(client, self.bucket, proof, 'correlation_list.txt', tmpdir)
            self.error = {
                'build': read_file('build-err.txt'),
                'property': read_file('cbmc-err.txt'),
                'coverage': read_file('coverage-err.txt'),
                'report': read_file('report-err.txt')
            }
        logging.info(' done')
        if self.log['property']:
            self.proof_status = self.log['property'][-2:]
        else:
            self.proof_status = "unknown"

    def summary(self, detail=1):
        result = {
            'correlation_id': self.correlation_id,
            'proof_status': self.proof_status,
            'error': self.error
        }
        if detail > 1:
            result['log'] = self.log
        if detail > 2:
            result['bucket'] = self.bucket
        return {self.proof: result}

class ProofResults:
    def __init__(self, session, proofs):
        self.results = {}
        for proof in proofs:
            self.results[proof] = ProofResult(session, proof)

    def summary(self, detail=1):
        report = {}
        for _, result in self.results.items():
            report = dict(report, **result.summary(detail))
        return {'CBMCLogs': report}


#TODO: get bucket name from stack outputs (after Jonathan adds it).
def proof_bucket(client):
    buckets = [bkt['Name'] for bkt in client.list_buckets()['Buckets']]
    proof_buckets = [bkt for bkt in buckets if bkt.endswith(('-proofs'))]
    proof_buckets = [bkt for bkt in proof_buckets if bkt]
    assert len(proof_buckets) == 1
    return proof_buckets[0]

def cbmc_file(client, bucket, proof, filename, tmpdir):
    try:
        key = '{}/out/{}'.format(proof, filename)
        logging.info("Attempting to read S3 key: " + str(key))
        path = os.path.join(tmpdir, filename)
        client.download_file(bucket, key, path)
        with open(path) as data:
            return data.read().splitlines()
    except botocore.exceptions.ClientError:
        logging.error("Unable to read S3 bucket/key: {}/{}  ".format(str(bucket), str(key)))
        return None


################################################################

################################################################
# MWW additions
################################################################
import time

def correlation_id_file(client, bucket, proof, filename, tmpdir):
    try:
        key = '{}/{}'.format(proof, filename)
        logging.info("Attempting to read S3 key: " + str(key))
        path = os.path.join(tmpdir, filename)
        client.download_file(bucket, key, path)
        with open(path) as data:
            return json.loads(data.read())[0]
    except botocore.exceptions.ClientError:
        logging.error("Unable to read S3 bucket/key: {}/{}  ".format(str(bucket), str(key)))
        return None

MAX_QUERY_RESULTS = 10000

def await_query_result(client, query_id):
    kwargs = {'queryId': query_id}
    result = client.get_query_results(**kwargs)
    while result['status'] in set(['Scheduled', 'Running']):
        time.sleep(0.5)
        result = client.get_query_results(**kwargs)
    if len(result['results']) == MAX_QUERY_RESULTS:
        logging.warning(f'Query results may be truncated for query id: {query_id}')
    logging.info(" done")
    return result


def query_result_to_list_dict(result):
    list_dict = []
    for log_event in result['results']:
        event_dict = {}
        for kvp in log_event:
            event_dict[kvp["field"]] = kvp["value"]
        list_dict.append(event_dict)
    return list_dict


def start_query(client, loggroupnames, query, starttime, endtime):
    kwargs = {'logGroupNames': loggroupnames,
              'startTime': starttime,
              'queryString': query,
              'limit': MAX_QUERY_RESULTS}
    if endtime:
        kwargs['endTime'] = endtime

    # start the query
    result = client.start_query(**kwargs)
    return result['queryId']


class CorrelationIds:
    def __init__(self, session, group, start_time, end_time):
        client = session.client("logs")
        log_groups = [group.webhook()]

        # correlation_list.0 is kind of an odd key, but it is how CloudWatch
        # manages hierarchical values.  It is the first value of a JSON list.
        query = ("fields correlation_list.0, @timestamp, @message "
                 "| filter ispresent(correlation_list.0) "
                 "| filter task_name = \"HandleWebhookLambda\" "
                 "| filter status like /COMPLETED/")
        logging.info('starting correlation ids query ')
        query_id = start_query(client, log_groups, query, start_time, end_time)
        result = await_query_result(client, query_id)
        self.correlation_ids = query_result_to_list_dict(result)

    def root_ids(self):
        return [elem['correlation_list.0'] for elem in self.correlation_ids]

    def summary(self, detail):
        summary_list = []
        for elem in self.correlation_ids:
            dict = {}
            try:
                timestamp = elem['@timestamp']
                message = json.loads(elem['@message'])
                webhook = WebHook(message['event'])
                summary = webhook.summary(detail)
            except:
                parse_json_fail('message', timestamp)
                summary = {}
            dict = {"correlation_id" : elem['correlation_list.0'],
                    "timestamp" : timestamp,
                    "webhook" : summary}

            summary_list.append(dict)
        summary_list.sort(key=lambda elem: elem['timestamp'])
        return {"correlation_ids" : summary_list}


def event_summary(event, detail):
    if detail == 1:
        return event['msg']['task_name']
    if detail == 2:
        return {'status': event['msg']['status'],
                'task_id': event['msg']['task_id'],
                'task_name': event['msg']['task_name'],
                'timestamp': event['ts']}
    else:
        return event


### Task tree related stuff.
class TaskTree():
    def __init__(self, key):
        self.key = key
        self.children = {}
        self.events = []

    def add_element(self, key_path, element):
        if not key_path:
            self.events.append(element)
        else:
            head, *tail = key_path
            if not (self.children.get(head)):
                self.children[head] = TaskTree(head)
            self.children[head].add_element(tail, element)

    def launch_events(self):
        return list(filter(lambda x: x['msg']['status'].startswith("LAUNCH"), self.events))

    def started_events(self):
        return list(filter(lambda x: x['msg']['status'].startswith("STARTED"), self.events))

    def completed_events(self):
        return list(filter(lambda x: x['msg']['status'].startswith("COMPLETED"), self.events))

    def failed_events(self):
        return list(filter(lambda x: "FAILED" in x['msg']['status'], self.events))

    def succeeded_events(self):
        return list(filter(lambda x: "COMPLETED:SUCCEEDED" in x['msg']['status'], self.events))

    def last_events(self):
        msgs = self.completed_events()
        if not msgs:
            msgs = self.started_events()
            if not msgs:
                msgs = self.launch_events()
        return msgs

    def tree_failed_events(self):
        child_failed = list(itertools.chain(*[child.tree_failed_events() for child in self.children.values()]))
        return self.failed_events() + child_failed

    def tree_succeeded_events(self):
        child_succeeded = list(itertools.chain(*[child.tree_succeeded_events() for child in self.children.values()]))
        return self.succeeded_events() + child_succeeded

    def tree_incomplete_events(self):
        incomplete = list(itertools.chain(*[child.tree_incomplete_events() for child in self.children.values()]))
        if not self.completed_events():
            incomplete.extend(self.last_events())
        return incomplete

    def summary(self, detail):
        dict = {}
        dict['events'] = [event_summary(event, detail) for event in (self.last_events() if detail == 1 else self.events)]
        dict['children'] = [child.summary(detail) for child in self.children.values()]

        if (detail > 2):
            dict['key'] = self.key

        return dict

def compute_tree_time(task_tree, minmax, val):
    time_list = [time_from_iso(elem['ts']) for elem in task_tree.events]
    val = functools.reduce(lambda init, y: y if (not init) else minmax(init, y), time_list, val)
    for child in task_tree.children.values():
        val = compute_tree_time(child, minmax, val)
    return val

def time_elapsed_in_ms(task_tree):
    min_val = compute_tree_time(task_tree, min, None)
    max_val = compute_tree_time(task_tree, max, None)
    difference = max_val - min_val if (min_val and max_val) else 0
    return difference

class TaskTreeFailureSummary():
    def __init__(self, session, task_tree, log_groups, start, end, max_log_entries):
        def sort_event(the_list):
            return sorted(the_list, key=lambda event: event['msg']['task_name'])
        self.session = session
        self.failed_events = sort_event(task_tree.tree_failed_events())
        self.incomplete_events = sort_event(task_tree.tree_incomplete_events())
        self.succeeded_events = sort_event(task_tree.tree_succeeded_events())
        self.log_groups = log_groups
        self.start = start
        self.end = end
        self.max_log_entries = max_log_entries
        self.time_elapsed_in_ms = time_elapsed_in_ms(task_tree)

    def proof_res(self, event):
        return re.search(r"([\S]+)(\d{8}-\d{6})-([a-z]*)$", event['msg']['task_name'])

    def failed_proofs(self, detail):
        search_events = self.failed_events + (self.incomplete_events if detail > 1 else [])
        proofs = [event for event in search_events if self.proof_res(event)]
        return proofs

    def failed_proof_roots(self, detail):
        search_events = self.failed_events + (self.incomplete_events if detail > 1 else [])
        proof_roots = {re.group(1)[:-1] for re in [self.proof_res(event) for event in search_events] if re}
        logging.info("proof roots: " + str(proof_roots))
        return list(proof_roots)

    def failed_batch_log(self, msg, detail):
        task_name = msg['task_name']
        task_id = msg['task_id']
        batch = self.session.client('batch')
        result = batch.describe_jobs(jobs=[task_id])
        jobs = result['jobs']
        if not jobs:
            error_string = """ERROR: task_id {} does not have an associated log.
            AWS Batch logs are eventually disposed after batch process has completed.
            """.format(task_id)
            msgs = [error_string]
            log_streams = []
        else:
            log_stream = jobs[0]['container']['logStreamName']
            log_streams = [log_stream]
            logging.info("reading failure log for: " + task_name)
            events = self.log_groups.read_stream_last(self.log_groups.batch(), log_stream, self.max_log_entries)
            msgs = [event['message'] for event in events['events']]

        return {
            'task_name': task_name,
            'task_id': task_id,
            'log_group': self.log_groups.batch(),
            'log_streams': log_streams,
            'log_messages': msgs
        }

    def failed_batch_logs(self, detail):
        search_events = self.failed_events + (self.incomplete_events if detail > 1 else [])
        if len(search_events) > 10:
            logging.warning("Truncating failed proof list to 10 entries")
            search_events = search_events[0:9]

        if detail > 1:
            return [self.failed_batch_log(event['msg'], detail) for event in search_events if self.proof_res(event)]
        else:
            return []

    def failed_lambda(self, lambda_groups, task_name, task_id):
        log_group, log_streams, log_items = self.log_groups.matching_streams(
            lambda_groups[task_name], "", self.start, self.end,
            'Scanning log entries for ' + task_name)

        request_id, in_lambda = (task_id, False) if task_id else ("dummy", True)
        log_msgs = []

        for item in log_items:
            if item['message'].startswith('START RequestId: {}'.format(task_id)):
                in_lambda = True
            if in_lambda:
                log_msgs.append(item['message'])
            if item['message'].startswith('END RequestId: {}'.format(task_id)):
                in_lambda = False

        return {
            'task_name': task_name,
            'task_id': task_id,
            'log_group': log_group,
            'log_streams': log_streams,
            'log_messages': log_msgs
        }

    def failed_lambdas(self, detail):
        lambda_groups = {'cbmc_ci_start:lambda_handler' : self.log_groups.invoke(),
                         'HandleWebhookLambda': self.log_groups.webhook()}

        search_events = self.failed_events + (self.incomplete_events if detail > 1 else [])
        lambda_failures = {(event['msg']['task_name'], event['msg']['task_id'])
                           for event in search_events if event['msg']['task_name'] in lambda_groups.keys()}

        logs = []
        for (task_name, task_id) in lambda_failures:
            log_record = self.failed_lambda(lambda_groups, task_name, task_id)
            logs.append(log_record)
        return logs

    def failed_codebuild(self, detail):

        search_events = self.failed_events + (self.incomplete_events if detail > 1 else [])
        codebuild_failures = {(event['msg']['task_name'], event['msg']['task_id'])
                              for event in search_events if event['msg']['task_name'] == "prepare_source:source_prepare"}

        logs = []
        for (task_name, task_id) in codebuild_failures:
            stream_id = task_id.split(":")[1]
            events = self.log_groups.read_stream_last(self.log_groups.prepare(), stream_id, self.max_log_entries)
            msgs = [event['message'] for event in events['events']]
            log_record = {
                'task_name': task_name,
                'task_id': task_id,
                'log_group': self.log_groups.prepare(),
                'log_stream': stream_id,
                'log_messages': msgs
            }
            logs.append(log_record)

        return logs


    def summary(self, detail, diagnose=False):
        dictionary = {}
        dictionary['failed_tasks'] = [event_summary(event, detail) for event in self.failed_events]
        dictionary['incomplete_tasks'] = [event_summary(event, detail) for event in self.incomplete_events]
        dictionary['succeeded_tasks'] = [event_summary(event, detail) for event in self.succeeded_events]
        dictionary['time_elapsed_in_ms'] = self.time_elapsed_in_ms
        if diagnose:
            dictionary['failed_lambda_logs'] = self.failed_lambdas(detail)
            dictionary['failed_codebuild_logs'] = self.failed_codebuild(detail)
            dictionary['failed_batch_logs'] = self.failed_batch_logs(detail)

        return dictionary

def create_task_tree(group, correlation_id, start_time, end_time):
    log_groups = [group.webhook(), group.invoke(), group.status(), group.prepare()]
    logging.info("Creating task tree")
    query = "fields @timestamp, @message | filter correlation_list.0 = \"{}\" | filter ispresent(status)".format(
        correlation_id)
    query_id = start_query(group.client, log_groups, query, start_time, end_time)
    result = await_query_result(group.client, query_id)
    list_dict = query_result_to_list_dict(result)
    if not list_dict:
        logging.info("No data for correlation_id {} during the specified interval".format(correlation_id))
        return TaskTree("NoData")

    # if we sort the keys in the dict, we have a depth-first view of the tree.
    task_tree = TaskTree("fakeroot")
    for elem in list_dict:
        ts = elem['@timestamp']
        try:
            msg = json.loads(elem['@message'])
        except:
            parse_json_fail('@message', elem['@timestamp'])
            msg = {}

        task_tree.add_element(msg['correlation_list'], {'ts': ts, 'msg': msg})

    # get 'real' root.
    assert len(task_tree.children) == 1
    return next(iter(task_tree.children.values()))

###########################################################


def main():
    args = create_parser().parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    logging.info('Arguments: %s', args)

    session = boto3.session.Session(profile_name=args.profile)
    try:
        log_groups = LogGroups(session)
    except requests.exceptions.HTTPError as error:
        if error.response.status_code == 401:
            sys.exit("\nAuthentication failed, reauthenticate and try again.\n")
        raise
    start, end = timestamp_interval(args.utc, args.interval)

    summary = {}
    if args.correlation_id:
        task_tree = create_task_tree(log_groups, args.correlation_id, start, end)
        failures = TaskTreeFailureSummary(session, task_tree, log_groups, start, end, args.max_log_entries)
        summary = dict(summary, **failures.summary(args.detail, args.diagnose))
        if args.task_tree:
            summary = dict(summary, **task_tree.summary(args.detail))

    if args.proofs:
        logging.info("Examining proofs: " + str(args.proofs))
        prepare = PrepareLogs(log_groups, args.proofs, start, end)
        proof_results = ProofResults(session, args.proofs)

        correlation_ids = CorrelationIds(session, log_groups, start, end)
        summary = dict(summary, **correlation_ids.summary(args.detail))
        if not correlation_ids.correlation_ids:
            invoke = InvokeLogs(log_groups, None, start, end)
            summary = dict(summary, **invoke.summary(args.detail))

        summary = dict(summary, **prepare.summary(args.detail))
        summary = dict(summary, **proof_results.summary(args.detail))

        if args.detail > 3:
            for proof in args.proofs:
                log = ProofBatchLog(log_groups, start, end, proof)
                summary = dict(summary, **log.summary(args.detail))

    if not args.correlation_id and not args.proofs:
        status = StatusLog(log_groups, start, end)
        summary = dict(summary, **status.summary(args.detail))
        proof_batch = ProofBatchStatus(session, start, end)
        summary = dict(summary, **proof_batch.summary(args.detail))
        correlation_ids = CorrelationIds(session, log_groups, start, end)
        summary = dict(summary, **correlation_ids.summary(args.detail))
        if not correlation_ids.correlation_ids:
            invoke = InvokeLogs(log_groups, None, start, end)
            summary = dict(summary, **invoke.summary(args.detail))
        if args.diagnose:
            failure_summaries = {}
            for root_id in correlation_ids.root_ids():
                task_tree = create_task_tree(log_groups, root_id, start, end)
                failures = TaskTreeFailureSummary(session, task_tree, log_groups, start, end, args.max_log_entries)
                failure_summaries[root_id] = failures.summary(args.detail, args.diagnose)
            summary['diagnoses'] = failure_summaries

    if (args.pprint):
        print(json.dumps(summary, indent=2))
    else:
        print(json.dumps(summary))

if __name__ == '__main__':
    main()

#proof --profile freertos --utc 2019-09-06T05:30:00 --proofs SkipNameField-20190906-053353
