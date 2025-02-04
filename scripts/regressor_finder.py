# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import concurrent.futures
import csv
import itertools
import os
import subprocess
from collections import defaultdict
from datetime import datetime
from logging import INFO, basicConfig, getLogger

import dateutil.parser
import hglib
import zstandard
from dateutil.relativedelta import relativedelta
from libmozdata import vcs_map
from microannotate import utils as microannotate_utils
from pydriller import GitRepository
from tqdm import tqdm

from bugbug import bugzilla, db, repository
from bugbug.models.defect_enhancement_task import DefectEnhancementTaskModel
from bugbug.models.regression import RegressionModel
from bugbug.utils import download_check_etag, retry

basicConfig(level=INFO)
logger = getLogger(__name__)


MAX_MODIFICATION_NUMBER = 50
# TODO: Set to 2 years and 6 months. If it takes too long, make the task work incrementally like microannotate-generate.
RELATIVE_START_DATE = relativedelta(days=49)
# Only needed because mercurial<->git mapping could be behind.
RELATIVE_END_DATE = relativedelta(days=3)

BUG_FIXING_COMMITS_DB = "data/bug_fixing_commits.json"
db.register(
    BUG_FIXING_COMMITS_DB,
    "https://index.taskcluster.net/v1/task/project.relman.bugbug_annotate.regressor_finder.latest/artifacts/public/bug_fixing_commits.json.zst",
    2,
)

BUG_INTRODUCING_COMMITS_DB = "data/bug_introducing_commits.json"
db.register(
    BUG_INTRODUCING_COMMITS_DB,
    "https://index.taskcluster.net/v1/task/project.relman.bugbug_annotate.regressor_finder.latest/artifacts/public/bug_introducing_commits.json.zst",
    1,
)

TOKENIZED_BUG_INTRODUCING_COMMITS_DB = "data/tokenized_bug_introducing_commits.json"
db.register(
    TOKENIZED_BUG_INTRODUCING_COMMITS_DB,
    "https://index.taskcluster.net/v1/task/project.relman.bugbug_annotate.regressor_finder.latest/artifacts/public/tokenized_bug_introducing_commits.json.zst",
    1,
)


BASE_URL = "https://index.taskcluster.net/v1/task/project.relman.bugbug.train_{model_name}.latest/artifacts/public/{model_name}model.zst"


def compress_file(path):
    cctx = zstandard.ZstdCompressor()
    with open(path, "rb") as input_f:
        with open(f"{path}.zst", "wb") as output_f:
            cctx.copy_stream(input_f, output_f)


def download_model(model_name):
    if not os.path.exists(f"{model_name}model"):
        url = BASE_URL.format(model_name=model_name)
        logger.info(f"Downloading {url}...")
        download_check_etag(url, f"{model_name}model.zst")
        dctx = zstandard.ZstdDecompressor()
        with open(f"{model_name}model.zst", "rb") as input_f:
            with open(f"{model_name}model", "wb") as output_f:
                dctx.copy_stream(input_f, output_f)
        assert os.path.exists(f"{model_name}model"), "Decompressed file exists"


class RegressorFinder(object):
    def __init__(
        self,
        cache_root,
        git_repo_url,
        git_repo_dir,
        tokenized_git_repo_url,
        tokenized_git_repo_dir,
    ):
        self.mercurial_repo_dir = os.path.join(cache_root, "mozilla-central")
        self.git_repo_url = git_repo_url
        self.git_repo_dir = git_repo_dir
        self.tokenized_git_repo_url = tokenized_git_repo_url
        self.tokenized_git_repo_dir = tokenized_git_repo_dir

        logger.info(f"Cloning mercurial repository to {self.mercurial_repo_dir}...")
        repository.clone(self.mercurial_repo_dir)

        logger.info(f"Cloning {self.git_repo_url} to {self.git_repo_dir}...")
        self.clone_git_repo(self.git_repo_url, self.git_repo_dir)
        logger.info(
            f"Cloning {self.tokenized_git_repo_url} to {self.tokenized_git_repo_dir}..."
        )
        self.clone_git_repo(self.tokenized_git_repo_url, self.tokenized_git_repo_dir)
        logger.info(f"Initializing mapping between git and mercurial commits...")
        self.init_mapping()

    def clone_git_repo(self, repo_url, repo_dir):
        if not os.path.exists(repo_dir):
            retry(
                lambda: subprocess.run(["git", "clone", repo_url, repo_dir], check=True)
            )

        retry(
            lambda: subprocess.run(
                ["git", "pull", repo_url, "master"],
                cwd=repo_dir,
                capture_output=True,
                check=True,
            )
        )

    def init_mapping(self):
        logger.info("Downloading Mercurial <-> git mapping file...")
        vcs_map.download_mapfile()

        self.tokenized_git_to_mercurial, self.mercurial_to_tokenized_git = microannotate_utils.get_commit_mapping(
            self.tokenized_git_repo_dir
        )

    def get_commits_to_ignore(self):
        commits_to_ignore = []

        # TODO: Make repository analyze all commits, even those to ignore, but add a field "ignore" or a function should_ignore that analyzes the commit data. This way we don't have to clone the Mercurial repository in this script.
        with hglib.open(self.mercurial_repo_dir) as hg:
            revs = repository.get_revs(hg, -10000)

        commits = repository.hg_log_multi(self.mercurial_repo_dir, revs)

        commits_to_ignore = []

        def append_commits_to_ignore(commits, type_):
            for commit in commits:
                commits_to_ignore.append({"rev": commit.node, "type": type_})

        append_commits_to_ignore(
            list(repository.get_commits_to_ignore(self.mercurial_repo_dir, commits)), ""
        )

        logger.info(
            f"{len(commits_to_ignore)} commits to ignore (excluding backed-out commits)"
        )

        append_commits_to_ignore(
            (commit for commit in commits if commit.backedoutby), "backedout"
        )

        logger.info(
            f"{len(commits_to_ignore)} commits to ignore (including backed-out commits)"
        )

        with open("commits_to_ignore.csv", "w") as f:
            writer = csv.DictWriter(f, fieldnames=["rev", "type"])
            writer.writeheader()
            writer.writerows(commits_to_ignore)

        return commits_to_ignore

    def find_bug_fixing_commits(self):
        logger.info("Downloading commits database...")
        db.download_version(repository.COMMITS_DB)
        if db.is_old_version(repository.COMMITS_DB) or not os.path.exists(
            repository.COMMITS_DB
        ):
            db.download(repository.COMMITS_DB, force=True)

        logger.info("Downloading bugs database...")
        db.download_version(bugzilla.BUGS_DB)
        if db.is_old_version(bugzilla.BUGS_DB) or not os.path.exists(bugzilla.BUGS_DB):
            db.download(bugzilla.BUGS_DB, force=True)

        logger.info("Download previous classifications...")
        db.download_version(BUG_FIXING_COMMITS_DB)
        if db.is_old_version(BUG_FIXING_COMMITS_DB) or not os.path.exists(
            BUG_FIXING_COMMITS_DB
        ):
            db.download(BUG_FIXING_COMMITS_DB, force=True)

        logger.info("Get previously classified commits...")
        prev_bug_fixing_commits = list(db.read(BUG_FIXING_COMMITS_DB))
        prev_bug_fixing_commits_nodes = set(
            bug_fixing_commit["rev"] for bug_fixing_commit in prev_bug_fixing_commits
        )
        logger.info(f"Already classified {len(prev_bug_fixing_commits)} commits...")

        # TODO: Switch to the pure Defect model, as it's better in this case.
        logger.info("Downloading defect/enhancement/task model...")
        download_model("defectenhancementtask")
        defect_model = DefectEnhancementTaskModel.load("defectenhancementtaskmodel")

        logger.info("Downloading regression model...")
        download_model("regression")
        regression_model = RegressionModel.load("regressionmodel")

        start_date = datetime.now() - RELATIVE_START_DATE
        end_date = datetime.now() - RELATIVE_END_DATE
        logger.info(
            f"Gathering bug IDs associated to commits (since {start_date} and up to {end_date})..."
        )
        commit_map = defaultdict(list)
        for commit in repository.get_commits():
            if commit["node"] in prev_bug_fixing_commits_nodes:
                continue

            commit_date = dateutil.parser.parse(commit["pushdate"])
            if commit_date < start_date or commit_date > end_date:
                continue

            commit_map[commit["bug_id"]].append(commit["node"])

        logger.info(
            f"{sum(len(commit_list) for commit_list in commit_map.values())} commits found, {len(commit_map)} bugs linked to commits"
        )
        assert len(commit_map) > 0

        def get_relevant_bugs():
            return (bug for bug in bugzilla.get_bugs() if bug["id"] in commit_map)

        bug_count = sum(1 for bug in get_relevant_bugs())
        logger.info(
            f"{bug_count} bugs in total, {len(commit_map) - bug_count} bugs linked to commits missing"
        )

        known_defect_labels = defect_model.get_labels()
        known_regression_labels = regression_model.get_labels()

        bug_fixing_commits = []

        def append_bug_fixing_commits(bug_id, type_):
            for commit in commit_map[bug_id]:
                bug_fixing_commits.append({"rev": commit, "type": type_})

        for bug in tqdm(get_relevant_bugs(), total=bug_count):
            # Ignore bugs which are not linked to the commits we care about.
            if bug["id"] not in commit_map:
                continue

            # If we know the label already, we don't need to apply the model.
            if (
                bug["id"] in known_regression_labels
                and known_regression_labels[bug["id"]] == 1
            ):
                append_bug_fixing_commits(bug["id"], "r")
                continue

            if bug["id"] in known_defect_labels:
                if known_defect_labels[bug["id"]] == "defect":
                    append_bug_fixing_commits(bug["id"], "d")
                else:
                    append_bug_fixing_commits(bug["id"], "e")
                continue

            if defect_model.classify(bug)[0] == "defect":
                if regression_model.classify(bug)[0] == 1:
                    append_bug_fixing_commits(bug["id"], "r")
                else:
                    append_bug_fixing_commits(bug["id"], "d")
            else:
                append_bug_fixing_commits(bug["id"], "e")

        db.append(BUG_FIXING_COMMITS_DB, bug_fixing_commits)
        compress_file(BUG_FIXING_COMMITS_DB)

        bug_fixing_commits = prev_bug_fixing_commits + bug_fixing_commits
        return [
            bug_fixing_commit
            for bug_fixing_commit in bug_fixing_commits
            if bug_fixing_commit["type"] in ["r", "d"]
        ]

    def find_bug_introducing_commits(
        self, bug_fixing_commits, commits_to_ignore, tokenized
    ):
        if tokenized:
            db_path = TOKENIZED_BUG_INTRODUCING_COMMITS_DB
            repo_dir = self.tokenized_git_repo_dir
        else:
            db_path = BUG_INTRODUCING_COMMITS_DB
            repo_dir = self.git_repo_dir

        def git_to_mercurial(rev):
            if tokenized:
                return self.tokenized_git_to_mercurial[rev]
            else:
                return vcs_map.git_to_mercurial(rev)

        def mercurial_to_git(rev):
            if tokenized:
                return self.mercurial_to_tokenized_git[rev]
            else:
                return vcs_map.mercurial_to_git(rev)

        logger.info("Download previously found bug-introducing commits...")
        db.download_version(db_path)
        if db.is_old_version(db_path) or not os.path.exists(db_path):
            db.download(db_path, force=True)

        logger.info("Get previously found bug-introducing commits...")
        prev_bug_introducing_commits = list(db.read(db_path))
        prev_bug_introducing_commits_nodes = set(
            bug_introducing_commit["bug_fixing_rev"]
            for bug_introducing_commit in prev_bug_introducing_commits
        )
        logger.info(
            f"Already classified {len(prev_bug_introducing_commits)} commits..."
        )

        hashes_to_ignore = set(commit["rev"] for commit in commits_to_ignore)

        with open("git_hashes_to_ignore", "w") as f:
            f.writelines(
                "{}\n".format(mercurial_to_git(commit["rev"]))
                for commit in commits_to_ignore
                if not tokenized or commit["rev"] in self.mercurial_to_tokenized_git
            )

        logger.info(f"{len(bug_fixing_commits)} commits to analyze")

        # Skip already found bug-introducing commits.
        bug_fixing_commits = [
            bug_fixing_commit
            for bug_fixing_commit in bug_fixing_commits
            if bug_fixing_commit["rev"] not in prev_bug_introducing_commits_nodes
        ]

        logger.info(
            f"{len(bug_fixing_commits)} commits left to analyze after skipping already analyzed ones"
        )

        bug_fixing_commits = [
            bug_fixing_commit
            for bug_fixing_commit in bug_fixing_commits
            if bug_fixing_commit["rev"] not in hashes_to_ignore
        ]
        logger.info(
            f"{len(bug_fixing_commits)} commits left to analyze after skipping the ones in the ignore list"
        )

        if tokenized:
            bug_fixing_commits = [
                bug_fixing_commit
                for bug_fixing_commit in bug_fixing_commits
                if bug_fixing_commit["rev"] in self.mercurial_to_tokenized_git
            ]
            logger.info(
                f"{len(bug_fixing_commits)} commits left to analyze after skipping the ones with no git hash"
            )

        def _init(git_repo_dir):
            global GIT_REPO
            GIT_REPO = GitRepository(git_repo_dir)

        def find_bic(bug_fixing_commit):
            git_fix_revision = mercurial_to_git(bug_fixing_commit["rev"])

            logger.info(f"Analyzing {git_fix_revision}...")

            commit = GIT_REPO.get_commit(git_fix_revision)

            # Skip huge changes, we'll likely be wrong with them.
            if len(commit.modifications) > MAX_MODIFICATION_NUMBER:
                return [None]

            bug_introducing_modifications = GIT_REPO.get_commits_last_modified_lines(
                commit, hashes_to_ignore_path=os.path.realpath("git_hashes_to_ignore")
            )
            logger.info(bug_introducing_modifications)

            bug_introducing_commits = []
            for bug_introducing_hashes in bug_introducing_modifications.values():
                for bug_introducing_hash in bug_introducing_hashes:
                    bug_introducing_commits.append(
                        {
                            "bug_fixing_rev": bug_fixing_commit["rev"],
                            "bug_introducing_rev": git_to_mercurial(
                                bug_introducing_hash
                            ),
                        }
                    )

            # Add an empty result, just so that we don't reanalyze this again.
            if len(bug_introducing_commits) == 0:
                bug_introducing_commits.append(
                    {
                        "bug_fixing_rev": bug_fixing_commit["rev"],
                        "bug_introducing_rev": "",
                    }
                )

            return bug_introducing_commits

        with concurrent.futures.ThreadPoolExecutor(
            initializer=_init, initargs=(repo_dir,), max_workers=os.cpu_count() + 1
        ) as executor:
            bug_introducing_commits = executor.map(find_bic, bug_fixing_commits)
            bug_introducing_commits = tqdm(
                bug_introducing_commits, total=len(bug_fixing_commits)
            )
            bug_introducing_commits = list(
                itertools.chain.from_iterable(bug_introducing_commits)
            )

        total_results_num = len(bug_introducing_commits)
        bug_introducing_commits = list(filter(None, bug_introducing_commits))
        logger.info(
            f"Skipped {total_results_num - len(bug_introducing_commits)} commits as they were too big"
        )

        db.append(db_path, bug_introducing_commits)
        compress_file(db_path)


def evaluate(bug_fixing_commits, bug_introducing_commits):
    logger.info("Building bug -> commits map...")
    bug_to_commits_map = defaultdict(list)
    for commit in tqdm(repository.get_commits()):
        bug_to_commits_map[commit["bug_id"]].append(commit["node"])

    bug_fixing_commits = set(
        bug_fixing_commit["rev"] for bug_fixing_commit in bug_fixing_commits
    )

    logger.info("Loading known regressors using regressed-by information...")
    known_regressors = {}
    for bug in tqdm(bugzilla.get_bugs()):
        if bug["regressed_by"]:
            known_regressors[bug["id"]] = bug["regressed_by"]
    logger.info(f"Loaded {len(known_regressors)} known regressors")

    fix_to_regressors_map = defaultdict(list)
    for bug_introducing_commit in bug_introducing_commits:
        if bug_introducing_commit["bug_introducing_rev"] == "":
            continue

        fix_to_regressors_map[bug_introducing_commit["bug_fixing_rev"]].append(
            bug_introducing_commit["bug_introducing_rev"]
        )

    logger.info("Measuring how many known regressors SZZ was able to find correctly...")
    all_regressors = 0
    perfect_regressors = 0
    found_regressors = 0
    misassigned_regressors = 0
    for bug_id, regressor_bugs in tqdm(known_regressors.items()):
        # Get all commits which fixed the bug.
        fix_commits = bug_to_commits_map[bug_id] if bug_id in bug_to_commits_map else []
        if len(fix_commits) == 0:
            continue

        # Skip bug/regressor when we didn't analyze the commits to fix the bug (as
        # certainly we can't have found the regressor in this case).
        if not any(fix_commit in bug_fixing_commits for fix_commit in fix_commits):
            continue

        # Get all commits linked to the regressor bug.
        regressor_commits = []
        for regressor_bug in regressor_bugs:
            if regressor_bug not in bug_to_commits_map:
                continue

            regressor_commits += (
                commit for commit in bug_to_commits_map[regressor_bug]
            )

        if len(regressor_commits) == 0:
            continue

        found_good = False
        found_bad = False
        for fix_commit in fix_commits:
            # Check if we found at least a correct regressor.
            if fix_commit in fix_to_regressors_map and any(
                regressor_commit in regressor_commits
                for regressor_commit in fix_to_regressors_map[fix_commit]
            ):
                found_good = True

            # Check if we found at least a wrong regressor.
            if fix_commit in fix_to_regressors_map and any(
                regressor_commit not in regressor_commits
                for regressor_commit in fix_to_regressors_map[fix_commit]
            ):
                found_bad = True

        all_regressors += 1

        if found_good and not found_bad:
            perfect_regressors += 1
        if found_good:
            found_regressors += 1
        if found_bad:
            misassigned_regressors += 1

    print(f"Perfectly found {perfect_regressors} regressors out of {all_regressors}")
    print(f"Found {found_regressors} regressors out of {all_regressors}")
    print(f"Misassigned {misassigned_regressors} regressors out of {all_regressors}")


def main():
    description = "Find bug-introducing commits from bug-fixing commits"
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument("cache_root", help="Cache for repository clones.")
    parser.add_argument(
        "git_repo_url", help="URL to the git repository on which to run SZZ."
    )
    parser.add_argument(
        "git_repo_dir", help="Path where the git repository will be cloned."
    )
    parser.add_argument(
        "tokenized_git_repo_url",
        help="URL to the tokenized git repository on which to run SZZ.",
    )
    parser.add_argument(
        "tokenized_git_repo_dir",
        help="Path where the tokenized git repository will be cloned.",
    )

    args = parser.parse_args()

    regressor_finder = RegressorFinder(
        args.cache_root,
        args.git_repo_url,
        args.git_repo_dir,
        args.tokenized_git_repo_url,
        args.tokenized_git_repo_dir,
    )

    commits_to_ignore = regressor_finder.get_commits_to_ignore()

    bug_fixing_commits = regressor_finder.find_bug_fixing_commits()

    regressor_finder.find_bug_introducing_commits(
        bug_fixing_commits, commits_to_ignore, True
    )
    evaluate(bug_fixing_commits, db.read(TOKENIZED_BUG_INTRODUCING_COMMITS_DB))

    regressor_finder.find_bug_introducing_commits(
        bug_fixing_commits, commits_to_ignore, False
    )
    evaluate(bug_fixing_commits, db.read(BUG_INTRODUCING_COMMITS_DB))
