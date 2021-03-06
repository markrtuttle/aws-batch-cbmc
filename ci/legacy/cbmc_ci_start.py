# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lambda function to invoke CBMC Batch upon a GitHub webhook event."""

from backports import tempfile
import boto3
import cbmc_batch
import cbmc_ci_github
from cbmc_ci_timer import Timer
import glob
import json
import os
from os.path import join
import sys
import time
import traceback
from yaml import load

# The name of the directory expected to exist in the GitHub repo that contains
# the a directory per proof; we used to use .cbmc-batch/jobs, but more recent
# projects should use cbmc/proofs
jobs_dirs = ["cbmc/proofs", ".cbmc-batch/jobs"]

# Expected name for CBMC Batch yaml
yaml_name = "cbmc-batch.yaml"

# S3 Bucket name for storing CBMC Batch packages and outputs
bkt = os.environ['S3_BKT']


def lambda_handler(event, context):
    """
    Start CBMC Batch jobs and update the GitHub commit status to "pending" for
    each job.

    event is a json data structure containing a header and a payload.
    The header format is described here: https://developer.github.com/webhooks/
    The request should be pre-processed by a Lambda function checking the
    X-Hub-Signature in the header.
    The payload, which contains a string that can be parsed into json for the
    type of GitHub event has a format as described here:
        https://developer.github.com/v3/activity/events/types/
    We expect to handle PushEvents and PullRequestEvents
    """
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Get GitHub event
            (name, full_name, sha, repo_id) = cbmc_ci_github.parse_event(event)
            # do nothing in case this was a push deleting a branch
            if sha is None:
                print "Ignoring event: " + json.dumps(event)
                return 0

            (src, tar_file) = cbmc_ci_github.get_tar(
                    name, full_name, sha, tmp_dir)

            # add the current directory to the PATH for cbmc_batch to invoke
            # the aws cli successfully
            os.environ['PATH'] = "{}:{}".format(
                    os.environ['LAMBDA_TASK_ROOT'], os.environ['PATH'])

            # Change to a directory that CBMC Batch can write to
            os.chdir(tmp_dir)

            # For every subdirectory in cbmc/proofs or .cbmc-batch/jobs
            tasks = []
            for d in jobs_dirs:
                src_d = join(src, d)
                if os.path.isdir(src_d):
                    for p in os.listdir(src_d):
                        if os.path.isdir(join(src_d, p)):
                            tasks.append((p, join(src_d, p)))
                else:
                    print "path {} is not an existing directory".format(src_d)
            print "{} tasks found".format(len(tasks))
            for (subdir, ws) in tasks:
                try:
                    # Try to run batch
                    (jobname, expected) = run_batch(
                            os.environ['AWS_REGION'], ws, src,
                            subdir, tar_file)
                    batch_bookkeep(
                            tmp_dir, repo_id, sha, expected, subdir,
                            jobname)
                except Exception as e:
                    # Update commit status to error
                    traceback.print_exc()
                    cbmc_ci_github.update_status(
                            "error", subdir, None,
                            "Problem launching verification", repo_id, sha)
                    print "Error: " + str(e)

    except Exception as e:
        traceback.print_exc()
        print "Error: " + str(e)
        raise e

    return 0


def run_batch(region, ws, src, task_name, tar_file):
    """Run the CBMC Batch job.

    Inputs: region - AWS region Batch is running in
            ws - workspace directory,
            src - source code directory,
            task_name - name of task
            tar_file - source archive file name
    Outputs: Expected result substring
    """
    # Expect a Makefile in the directory
    if not os.path.isfile(join(ws, "Makefile")):
        raise ValueError("Missing Makefile from " + ws)

    # Expected CBMC output contains expected_result as a substring
    expected = ""

    # Expect yaml_name in the directory
    yamls = glob.glob(join(ws, "*.yaml"))
    yaml = join(ws, yaml_name)
    if yaml in yamls:
        # Bookkeep expected result
        with open(yaml, "r") as stream:
            expected = expected_result(load(stream))
    else:
        raise ValueError("Missing " + yaml_name + " from " + ws)

    # fix the jobname now, in the same way that cbmc_batch would do
    gmt = time.gmtime()
    timestamp_str = ("{:04d}{:02d}{:02d}-{:02d}{:02d}{:02d}"
                     .format(gmt.tm_year, gmt.tm_mon, gmt.tm_mday,
                             gmt.tm_hour, gmt.tm_min, gmt.tm_sec))
    jobname = task_name + "-" + timestamp_str

    # CBMC Batch args -- require that property-checking is performed
    cbmc_batch.sys.argv = [
            "cbmc_batch",
            "--region", region,
            "--no-file-output",
            "--wsdir", ws,
            "--srcdir", src, "--no-copysrc",
            "--srctarfile",
            "s3://{}/{}".format(bkt, tar_file),
            "--bucket", bkt,
            "--jobname", jobname,
            "--taskname", task_name,
            "--yaml", yaml]

    # Run CBMC Batch
    timer = Timer("Run CBMC Batch")
    cbmc_batch.main()
    timer.end()

    # Return expected result for bookkeeping
    return (jobname, expected)


def batch_bookkeep(tmp_dir, repo_id, sha, expected, subdir, batch_name):
    # Bookkeeping about the GitHub commit for later response
    bookkeep(tmp_dir, batch_name, repo_id, "repo_id.txt")
    bookkeep(tmp_dir, batch_name, sha, "sha.txt")
    # Bookkeeping about expected result for later response
    bookkeep(tmp_dir, batch_name, expected, "expected.txt")
    # Update commit status to pending
    desc = "Verification Pending: CBMC Batch job " + batch_name
    cbmc_ci_github.update_status(
            "pending", subdir, batch_name, desc, repo_id, sha)


def expected_result(yaml):
    """Return an expected substring for the CBMC result

    The result is specified in a dict constructed from a user-provided yaml
    """
    if "expected" in yaml.keys():
        return yaml["expected"]
    return ""


def bookkeep(tmp_dir, job_name, content, file_name):
    """Upload tmp_dir/file_name to the S3 bucket as job_name/file_name"""
    file_path = join(tmp_dir, file_name)
    with open(file_path, "w") as file_obj:
        file_obj.write(str(content))
    s3 = boto3.client('s3')
    s3.upload_file(
            Bucket=bkt, Key=job_name + "/" + file_name, Filename=file_path)
