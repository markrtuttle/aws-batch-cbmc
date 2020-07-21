# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lambda function to invoke CBMC Batch upon a GitHub webhook event."""

import tarfile
import re
import glob
import json
import os
from os.path import join
import time
import traceback
from yaml import load
import boto3



import clog_writert
import cbmc_batch
import cbmc_ci_github
from cbmc_ci_timer import Timer

# The name of the directory expected to exist in the GitHub repo that contains
# the a directory per proof; we used to use .cbmc-batch/jobs, but more recent
# projects should use cbmc/proofs
jobs_dirs = ["cbmc/proofs", ".cbmc-batch/jobs"]

# Expected name for CBMC Batch yaml
yaml_name = "cbmc-batch.yaml"

# S3 Bucket name for storing CBMC Batch packages and outputs
# FIX: Lambdas put S3_BKT in env, CodeBuild puts S3_BUCKET in env.
bkt_proofs = os.environ.get('S3_BUCKET_PROOFS')
bkt_tools = os.environ.get('S3_BUCKET_TOOLS')

def scan_tarfile_for_proofs(tarfile_name, proof_markers):
    """Return a list of (proof_root, proof_subdir) pairs for every proof
    directory found in the tar file.

    A proof directory is any directory under one of the proof markers
    (expected to be 'cbmc/proofs' and '.cbmc-batch/jobs') containing a
    file named 'cbmc-batch.yaml'.
    """
    print("Scanning '{}' for CBMC proofs".format(tarfile_name))

    proofs = []
    try:
        with tarfile.open(tarfile_name) as tar:
            for tarinfo in tar.getmembers():
                name = tarinfo.name
                for proof_marker in proof_markers:
                    match = re.match(
                        "(.*/{})/(.*)/cbmc-batch.yaml".format(proof_marker),
                        name)
                    if match:
                        proofs.append((match.group(1), match.group(2)))
                        break
    except (tarfile.ReadError, IOError) as err:
        print("Couldn't scan '{}' for CBMC proofs: {}".format(tarfile_name,
                                                              str(err)))
        return None
    return proofs

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

    #pylint: disable=unused-argument,too-many-locals
    logger = clog_writert.CLogWriter.init_lambda("cbmc_ci_start:lambda_handler", event, context)
    logger.started()
    try:
        # Get GitHub event
        (name, id, branch, sha, is_draft) = cbmc_ci_github.parse_event(event)

        # do nothing in case this was a push deleting a branch
        if sha is None:
            print("Ignoring event: " + json.dumps(event))
            logger.summary(clog_writert.SUCCEEDED, event, {'result': 'Ignoring event with empty SHA'})
            return 0

        to_check = [
            "master",
            "release-1.5",
            "http",
            "http_dev",
            "release-candidate",
            "COMMENT_RETRY"  # Dummy branch that indicates this was a retry trigger
        ]
        branch_tail = branch.split('/')[-1]

        if (name == "aws/amazon-freertos" and branch_tail not in to_check):
            result = f"Ignoring event on repository {name} and branch {branch}: {json.dumps(event)}"
            print(result)
            logger.summary(clog_writert.SUCCEEDED, event, {'result': result})
            return 0

        child_correlation_list = logger.create_child_correlation_list()
        codebuild = boto3.client('codebuild')
        result = codebuild.start_build(
            projectName='Prepare-Source-Project',
            environmentVariablesOverride=[
                {
                    'name': 'CBMC_REPOSITORY',
                    'value': "https://github.com/" + name,
                    'type': 'PLAINTEXT'
                },
                {
                    'name': 'CBMC_SHA',
                    'value': sha,
                    'type': 'PLAINTEXT'
                },
                {
                    'name': 'CBMC_IS_DRAFT',
                    'value': str(is_draft),
                    'type': 'PLAINTEXT'
                },
                {
                    'name': 'CBMC_ID',
                    'value': str(id),
                    'type': 'PLAINTEXT'
                },
                {
                    'name': 'CORRELATION_LIST',
                    'value': json.dumps(child_correlation_list),
                    'type': 'PLAINTEXT'
                }
            ]
        )
        child_task_id = result['build']['id']
        logger.launch_child("prepare_source:source_prepare", child_task_id, child_correlation_list)
        response = {'id' : child_task_id}
        logger.summary(clog_writert.SUCCEEDED, event, response)
    except Exception as e:
        traceback.print_exc()
        print("Error: " + str(e))
        response = {}
        response['error'] = "Exception: {}; Traceback: {}".format(str(e), traceback.format_exc())
        logger.summary(clog_writert.FAILED, event, response)
        raise e

    return 0

def generate_cbmc_makefiles(group_names, topdir):
    ran = False
    for directory in find_proof_groups(group_names, topdir):
        files = os.listdir(directory)
        if "make-common-makefile.py" in files and "make-proof-makefiles.py" in files:
            cwd = os.getcwd()
            os.chdir(directory)
            print("Running make-common-makefile.py in {}".format(directory))
            os.system("python make-common-makefile.py")
            print("Running make-proof-makefiles.py in {}".format(directory))
            os.system("python make-proof-makefiles.py")
            os.chdir(cwd)
            ran = True
    return ran

def find_proof_groups(group_names, topdir='.'):
    """Find CBMC proof group directories under topdir.

    CBMC proofs are grouped together under directories with names like
    'cbmc/proofs' and '.cbmc-batch/jobs' given by the list group_names.
    Find all such directories under topdir.
    """

    return [path
            for path, _, _ in os.walk(topdir)
            if any([path.endswith(os.path.sep + suffix)
                    for suffix in group_names])]

def find_proofs(groupdir, relative=True):
    """Find CBMC proof directories under a proof group directory groupdir.

    A CBMC proof requires a file named 'cbmc-batch.yaml' that gives
    parameters for running that proof under CBMC Batch.  Find all such
    directories under groupdir.
    """

    prefix_length = len(groupdir)+1 if relative else 0
    return [dir[prefix_length:]
            for dir, _, files in os.walk(groupdir)
            if 'cbmc-batch.yaml' in files]

def find_tasks(group_names, topdir='.'):
    """Return (proof-name, proof-directory) pairs for CBMC proofs under topdir.

    For any given proof, CBMC Batch needs the name of the proof and the
    directory containing the proof.  Suppose groupdir is the group
    directory and proofdir is the subdirectory containing the proof.  The
    proof name is proofdir (with '/' replaced with '-').  The proof
    directory is the full path groupdir/proofdir.
    """

    return [(proofdir.replace(os.path.sep, '-'),
             os.path.join(groupdir, proofdir))
            for groupdir in find_proof_groups(group_names, topdir)
            for proofdir in find_proofs(groupdir)]

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
        "s3://{}/{}".format(bkt_proofs, tar_file),
        "--bucket", bkt_proofs,
        "--jobname", jobname,
        "--taskname", task_name,
        "--yaml", yaml]
    # FIX: Lambdas put PKG_BKT in env, CodeBuild puts S3_PKG_PATH in env.
    if os.environ.get('PKG_BKT'):
        cbmc_batch.sys.argv += ["--pkgbucket", os.environ['PKG_BKT']]
    elif os.environ.get('S3_BUCKET_TOOLS') and os.environ.get('S3_PKG_PATH'):
        cbmc_batch.sys.argv += ["--pkgbucket",
                                "{}/{}".format(os.environ['S3_BUCKET_TOOLS'], os.environ['S3_PKG_PATH'])]

    # Run CBMC Batch
    timer = Timer("Run CBMC Batch")
    print("CBMC Batch options")
    print(json.dumps(cbmc_batch.sys.argv))
    cbmc_batch.main()
    timer.end()

    # Return expected result for bookkeeping
    return (jobname, expected)


def batch_bookkeep(
        tmp_dir, repo_id, sha, is_draft, expected, subdir, batch_name, correlation_list):
    #pylint: disable=too-many-arguments

    # Bookkeeping about the GitHub commit for later response
    bookkeep(tmp_dir, batch_name, repo_id, "repo_id.txt")
    bookkeep(tmp_dir, batch_name, sha, "sha.txt")
    bookkeep(tmp_dir, batch_name, is_draft, "is_draft.txt")
    # Bookkeeping about expected result for later response
    bookkeep(tmp_dir, batch_name, expected, "expected.txt")
    bookkeep(tmp_dir, batch_name, correlation_list, "correlation_list.txt")
    # Update commit status to pending
    desc = "Verification Pending: CBMC Batch job " + batch_name
    cbmc_ci_github.update_status(
        "pending", subdir, batch_name, desc, repo_id, sha, False)


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
        Bucket=bkt_proofs, Key=job_name + "/" + file_name, Filename=file_path)
