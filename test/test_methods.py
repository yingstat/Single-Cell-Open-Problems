import utils

import parameterized
import tempfile
import os
import functools
import json
import datetime
import time
import atexit

import openproblems

utils.warnings.ignore_warnings()

TESTDIR = os.path.dirname(os.path.abspath(__file__))
BASEDIR = os.path.dirname(TESTDIR)
CACHEDIR = os.path.join(os.environ["HOME"], ".singularity")
os.environ["SINGULARITY_CACHEDIR"] = CACHEDIR
os.environ["SINGULARITY_PULLFOLDER"] = CACHEDIR


def docker_paths(image):
    """Get relevant paths for a Docker image."""
    docker_path = os.path.join(BASEDIR, "docker", image)
    docker_push = os.path.join(docker_path, ".docker_push")
    dockerfile = os.path.join(docker_path, "Dockerfile")
    requirements = [
        os.path.join(docker_path, f)
        for f in os.listdir(docker_path)
        if f.endswith("requirements.txt")
    ]
    return docker_push, dockerfile, requirements


def build_docker(image):
    """Build a Docker image."""
    _, dockerfile, _ = docker_paths(image)
    utils.run.run(
        [
            "docker",
            "build",
            "-f",
            dockerfile,
            "-t",
            "singlecellopenproblems/{}".format(image),
            BASEDIR,
        ],
        print_stdout=True,
    )


@functools.lru_cache(maxsize=None)
def docker_available():
    """Check if Docker can be run."""
    returncode = utils.run.run(["docker", "images"], return_code=True)
    return returncode == 0


def docker_image_age(image):
    """Check when the Docker image was built."""
    assert docker_available()
    image_info, returncode = utils.run.run(
        ["docker", "inspect", "singlecellopenproblems/{}:latest".format(image)],
        return_stdout=True,
        return_code=True,
    )
    if not returncode == 0:
        # image not available
        return 0
    else:
        image_dict = json.loads(image_info)[0]
        created_time = image_dict["Created"].split(".")[0]
        created_timestamp = datetime.datetime.strptime(
            created_time, "%Y-%m-%dT%H:%M:%S"
        ).timestamp()
        return created_timestamp


def docker_push_age(filename):
    """Check when the Docker image was last pushed to Docker Hub."""
    try:
        with open(filename, "r") as handle:
            return float(handle.read().strip())
    except FileNotFoundError:
        return 0


@functools.lru_cache(maxsize=None)
def image_requires_docker(image):
    """Check if a specific image requires Docker or Singularity.

    If the image has been modified more recently than it was pushed to Docker Hub, then
    it should be run in Docker. Otherwise, we use Singularity.
    """
    docker_push, dockerfile, requirements = docker_paths(image)
    push_timestamp = docker_push_age(docker_push)
    git_file_age = utils.run.git_file_age(dockerfile)
    for req in requirements:
        req_age = utils.run.git_file_age(req)
        git_file_age = max(git_file_age, req_age)
    if push_timestamp > git_file_age:
        return False
    else:
        if not docker_available():
            raise RuntimeError(
                "The Dockerfile for image {} is newer than the "
                "latest push, but Docker is not available."
            )
        if docker_image_age(image) < git_file_age:
            import sys

            print(
                "Building {}:\n"
                "Docker push age: {}\n"
                "Docker image modified: {}\n"
                "Docker image age: {}".format(
                    image, push_timestamp, git_file_age, docker_image_age(image)
                ),
                file=sys.stderr,
            )
            sys.stderr.flush()
            build_docker(image)
        return True


@functools.lru_cache(maxsize=None)
def cache_singularity_image(image):
    """Download a Singularity image from Dockerhub."""
    docker_push, _, _ = docker_paths(image)
    push_timestamp = docker_push_age(docker_push)
    image_filename = "{}.sif".format(image)
    image_path = os.path.join(CACHEDIR, image_filename)
    image_age_filename = os.path.join(CACHEDIR, "{}.age.txt".format(image))
    image_age = docker_push_age(image_age_filename)
    if push_timestamp > image_age and os.path.isfile(image_path):
        os.remove(image_path)
    if not os.path.isfile(image_path):
        utils.run.run(
            [
                "singularity",
                "--verbose",
                "pull",
                "--name",
                image_filename,
                "docker://singlecellopenproblems/{}".format(image),
            ],
        )
        with open(image_age_filename, "w") as handle:
            handle.write(str(time.time()))
    return image_path


def singularity_command(image, script, *args):
    """Get the Singularity command to run a script."""
    return [
        "singularity",
        "--verbose",
        "exec",
        "-B",
        "{0}:{0}".format(BASEDIR),
        cache_singularity_image(image),
        "/bin/bash",
        "{}/test/singularity_run.sh".format(BASEDIR),
        "{}/test".format(BASEDIR),
        script,
    ] + list(args)


@functools.lru_cache(maxsize=None)
def cache_docker_image(image):
    """Run a Docker image and get the machine ID."""
    hash = utils.run.run(
        [
            "docker",
            "run",
            "-dt",
            "--rm",
            "--mount",
            "type=bind,source={0},target={0}".format(BASEDIR),
            "--mount",
            "type=bind,source=/tmp,target=/tmp",
            "singlecellopenproblems/{}".format(image),
        ],
        return_stdout=True,
    )
    container = hash[:12]

    def stop():
        utils.run.run(["docker", "stop", container])

    atexit.register(stop)
    return container


def docker_command(image, script, *args):
    """Get the Docker command to run a script."""
    container = cache_docker_image(image)
    run_command = [
        "docker",
        "exec",
        container,
        "/bin/bash",
        "{}/test/singularity_run.sh".format(BASEDIR),
        "{}/test/".format(BASEDIR),
        script,
    ] + list(args)
    return run_command


def run_image(image, *args):
    """Run a Python script in a container."""
    if image_requires_docker(image) or docker_available():
        container_command = docker_command
    else:
        container_command = singularity_command
    utils.run.run(container_command(image, *args))


@parameterized.parameterized.expand(
    [
        (task, dataset, method)
        for task in openproblems.TASKS
        for dataset in task.DATASETS
        for method in task.METHODS
    ],
    name_func=utils.name.name_test,
)
def test_method(task, dataset, method):
    """Test application of a method."""
    task_name = task.__name__.split(".")[-1]
    with tempfile.NamedTemporaryFile(suffix=".h5ad") as data_file:
        run_image(
            method.metadata["image"],
            "run_test_method.py",
            task_name,
            method.__name__,
            dataset.__name__,
            data_file.name,
        )
        for metric in task.METRICS:
            run_image(
                metric.metadata["image"],
                "run_test_metric.py",
                task_name,
                metric.__name__,
                data_file.name,
            )


@parameterized.parameterized.expand(
    [(method,) for task in openproblems.TASKS for method in task.METHODS],
    name_func=utils.name.name_test,
)
def test_method_metadata(method):
    """Test for existence of method metadata."""
    assert hasattr(method, "metadata")
    for attr in [
        "method_name",
        "paper_name",
        "paper_url",
        "paper_year",
        "code_url",
        "code_version",
        "image",
    ]:
        assert attr in method.metadata
