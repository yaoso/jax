# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bazel macros used by the JAX build."""

load("@org_tensorflow//tensorflow/core/platform/default:build_config.bzl", _pyx_library = "pyx_library")
load("@org_tensorflow//tensorflow:tensorflow.bzl", _if_windows = "if_windows", _pybind_extension = "pybind_extension")
load("@local_config_cuda//cuda:build_defs.bzl", _cuda_library = "cuda_library", _if_cuda_is_configured = "if_cuda_is_configured")
load("@local_config_rocm//rocm:build_defs.bzl", _if_rocm_is_configured = "if_rocm_is_configured", _rocm_library = "rocm_library")
load("@flatbuffers//:build_defs.bzl", _flatbuffer_cc_library = "flatbuffer_cc_library")

# Explicitly re-exports names to avoid "unused variable" warnings from .bzl
# lint tools.
cuda_library = _cuda_library
rocm_library = _rocm_library
pytype_library = native.py_library
pyx_library = _pyx_library
pybind_extension = _pybind_extension
if_cuda_is_configured = _if_cuda_is_configured
if_rocm_is_configured = _if_rocm_is_configured
if_windows = _if_windows
flatbuffer_cc_library = _flatbuffer_cc_library

jax_internal_packages = []
jax_test_util_visibility = []
loops_visibility = []
sharded_jit_visibility = []

absl_logging_py_deps = []
absl_testing_py_deps = []
cloudpickle_py_deps = []
numpy_py_deps = []
pil_py_deps = []
portpicker_py_deps = []
scipy_py_deps = []
tensorflow_py_deps = []

jax_extra_deps = []
jax2tf_deps = []

def py_library_providing_imports_info(*, name, lib_rule = native.py_library, **kwargs):
    lib_rule(name = name, **kwargs)

def py_extension(name, srcs, copts, deps):
    pybind_extension(name, srcs = srcs, copts = copts, deps = deps, module_name = name)

def windows_cc_shared_mlir_library(name, out, deps = [], srcs = []):
    """Workaround DLL building issue.

    1. cc_binary with linkshared enabled cannot produce DLL with symbol
       correctly exported.
    2. Even if the DLL is correctly built, the resulting target cannot be
       correctly consumed by other targets.

    Args:
      name: the name of the output target
      out: the name of the output DLL filename
      deps: deps
      srcs: srcs
    """

    # create a dummy library to get the *.def file
    dummy_library_name = name + ".dummy.dll"
    native.cc_binary(
        name = dummy_library_name,
        linkshared = 1,
        linkstatic = 1,
        deps = deps,
    )

    # .def file with all symbols, not usable
    full_def_name = name + ".full.def"
    native.filegroup(
        name = full_def_name,
        srcs = [dummy_library_name],
        output_group = "def_file",
    )

    # filtered def_file, only the needed symbols are included
    filtered_def_name = name + ".filtered.def"
    filtered_def_file = out + ".def"
    native.genrule(
        name = filtered_def_name,
        srcs = [full_def_name],
        outs = [filtered_def_file],
        cmd = """echo 'LIBRARY {}\nEXPORTS ' > $@ && grep '^\\W*mlir' $(location :{}) >> $@""".format(out, full_def_name),
    )

    # create the desired library
    native.cc_binary(
        name = out,  # this name must be correct, it will be the filename
        linkshared = 1,
        deps = deps,
        win_def_file = filtered_def_file,
    )

    # however, the created cc_library (a shared library) cannot be correctly
    # consumed by other cc_*...
    interface_library_file = out + ".if.lib"
    native.filegroup(
        name = interface_library_file,
        srcs = [out],
        output_group = "interface_library",
    )

    # but this one can be correctly consumed, this is our final product
    native.cc_import(
        name = name,
        interface_library = interface_library_file,
        shared_library = out,
    )

def jax_test(
        name,
        srcs,
        args = [],
        shard_count = None,
        deps = [],
        disable_backends = None,  # buildifier: disable=unused-variable
        backend_tags = {},  # buildifier: disable=unused-variable
        disable_configs = None,  # buildifier: disable=unused-variable
        enable_configs = None,  # buildifier: disable=unused-variable
        tags = [],
        main = None):
    if shard_count == None or type(shard_count) == type(0):
        shards = shard_count
    else:
        shards = shard_count.get("cpu", None)
    native.py_test(
        name = name,
        srcs = srcs,
        args = args,
        deps = [
            "//jax",
            "//jax:test_util",
        ] + deps,
        shard_count = shards,
        tags = tags,
        main = main,
    )

def jax_generate_backend_suites():
    pass

jax_test_file_visibility = []
