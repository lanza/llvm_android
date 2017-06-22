#!/usr/bin/env python
#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import build
import compiler_wrapper
import multiprocessing
import os
import utils
import shutil
import subprocess

TARGETS = ('aosp_angler-eng', 'aosp_bullhead-eng', 'aosp_marlin-eng')


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('android_path', help='Android source directory.')
    parser.add_argument('clang_path', nargs='?', help='Clang toolchain '
                        'directory. If not specified, a new toolchain will '
                        'be built from scratch.')
    parser.add_argument('-k', '--keep-going', action='store_true',
                        default=False, help='Keep going when some targets '
                        'cannot be built.')
    parser.add_argument('-j', action='store', dest='jobs', type=int,
                        default=multiprocessing.cpu_count(),
                        help='Number of executed jobs.')
    parser.add_argument('--build-only', action='store_true',
                        default=False, help='Build default targets only.')
    parser.add_argument('--flashall-path', nargs='?', help='Use internal '
                        'flashall tool if the path is set.')
    parser.add_argument('-t', '--target', nargs='?', help='Build for specified '
                        'target. This will work only when --build-only is '
                        'enabled.')
    clean_built_target_group = parser.add_mutually_exclusive_group()
    clean_built_target_group.add_argument(
        '--clean-built-target', action='store_true', default=True,
        help='Clean output for each target that is built.')
    clean_built_target_group.add_argument(
        '--no-clean-built-target', action='store_false',
        dest='clean_built_target', help='Do not remove target output.')
    redirect_stderr_group = parser.add_mutually_exclusive_group()
    redirect_stderr_group.add_argument(
        '--redirect-stderr', action='store_true', default=True,
        help='Redirect clang stderr to $OUT/clang-error.log.')
    clean_built_target_group.add_argument(
        '--no-redirect-stderr', action='store_false',
        dest='redirect_stderr', help='Do not redirect clang stderr.')
    return parser.parse_args()


def link_clang(android_base, clang_path):
    android_clang_path = os.path.join(android_base, 'prebuilts', 'clang',
                                      'host', utils.build_os_type(),
                                      'clang-dev')
    utils.remove(android_clang_path)
    os.symlink(os.path.abspath(clang_path), android_clang_path)


def get_connected_device_list():
    try:
        # Get current connected device list.
        out = subprocess.check_output(['adb', 'devices', '-l'])
        devices = [x.split() for x in  out.strip().split('\n')[1:]]
        return devices
    except subprocess.CalledProcessError:
        # If adb is not working properly. Return empty list.
        return []


def rm_current_product_out():
    if 'ANDROID_PRODUCT_OUT' in os.environ:
        utils.remove(os.environ['ANDROID_PRODUCT_OUT'])


def build_target(android_base, clang_version, target, max_jobs,
                 redirect_stderr):
    jobs = '-j{}'.format(
            max(1, min(max_jobs, multiprocessing.cpu_count())))
    result = True
    env_out = subprocess.Popen(['bash', '-c',  '. ./build/envsetup.sh;'
                                'lunch ' + target + ' >/dev/null && env'],
                               cwd=android_base, stdout=subprocess.PIPE )
    env = {}
    for line in env_out.stdout:
        (key, _, value) = line.partition('=')
        value = value.strip()
        env[key] = value
    env_out.communicate()

    if redirect_stderr:
        redirect_key = compiler_wrapper.STDERR_REDIRECT_KEY
        redirect_path = os.path.abspath(os.path.join(android_base, 'out',
                                                     'clang-error.log'))
        utils.remove(redirect_path)
        env[redirect_key] = redirect_path

    fallback_path = build.clang_prebuilt_bin_dir()
    env[compiler_wrapper.PREBUILT_COMPILER_PATH_KEY] = fallback_path

    # http://b/62869798, we need to invoke cpp-define-generator manually to
    # avoid potential build failure. This should be removed when
    # art/runtime/generated/asm_support_gen.h is updated.
    subprocess.check_call(['/bin/bash', '-c',
                           'make cpp-define-generator-data ' + jobs +
                           ' dist LLVM_PREBUILTS_VERSION=clang-dev' +
                           (' LLVM_RELEASE_VERSION=%s' %
                            clang_version.short_version())], cwd=android_base,
                          env=env)
    asm_support_path = os.path.join(android_base, 'art', 'tools',
                                    'cpp-define-generator')
    subprocess.check_call(['./generate-asm-support'], cwd=asm_support_path,
                          env=env)

    print('Start building %s.' % target)
    subprocess.check_call(['/bin/bash', '-c', 'make ' + jobs + ' dist'
                           ' LLVM_PREBUILTS_VERSION=clang-dev' +
                           (' LLVM_RELEASE_VERSION=%s' %
                            clang_version.short_version())], cwd=android_base,
                          env=env)


def test_device(android_base, clang_version, device, max_jobs, clean_output,
                flashall_path, redirect_stderr):
    [label, target] = device[-1].split(':')
    # If current device is not connected correctly we will just skip it.
    if label != 'device':
        print('Device %s is not connecting correctly.' % device[0])
        return True
    else:
        target = 'aosp_' + target + '-eng'
    try:
        build_target(android_base, clang_version, target, max_jobs,
                     redirect_stderr)
        if flashall_path is None:
            bin_path = os.path.join(android_base, 'out', 'host',
                                    utils.build_os_type(), 'bin')
            subprocess.check_call(['./adb', '-s', device[0], 'reboot',
                                   'bootloader'], cwd=bin_path)
            subprocess.check_call(['./fastboot', '-s', device[0], 'flashall'],
                                  cwd=bin_path)
        else:
            os.environ['ANDROID_SERIAL'] = device[0]
            subprocess.check_call(['./flashall'], cwd=flashall_path)
        result = True
    except subprocess.CalledProcessError:
        print('Flashing/testing android for target %s failed!' % target)
        result = False
    if clean_output:
        rm_current_product_out()
    return result


def install_wrappers(llvm_install_path):
    wrapper_path = utils.llvm_path('android', 'compiler_wrapper.py')
    bin_path = os.path.join(llvm_install_path, 'bin')
    clang_path = os.path.join(bin_path, 'clang')
    clangxx_path = os.path.join(bin_path, 'clang++')

    # Rename clang and clang++ to clang.real and clang++.real. Clang may
    # already be moved, so we only move clang when clang is link.
    if os.path.islink(clang_path):
        shutil.move(clang_path, clang_path + '.real')
    utils.remove(clangxx_path)
    utils.remove(clangxx_path + '.real')
    os.symlink('clang.real', clangxx_path + '.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)


def build_clang():
    stage1_install = build.build_stage1()
    stage2_install = build.build_stage2(stage1_install, build.STAGE2_TARGETS)
    build.build_runtimes(stage2_install)
    version = build.extract_clang_version(stage2_install)
    return stage2_install, version


def main():
    args = parse_args()
    if args.clang_path is None:
        clang_path, clang_version = build_clang()
    else:
        clang_path = args.clang_path
        clang_version = build.extract_clang_version(clang_path)
    install_wrappers(clang_path)
    link_clang(args.android_path, clang_path)

    if args.build_only:
        targets = [args.target] if args.target else TARGETS
        for target in targets:
            build_target(args.android_path, clang_version, target,
                         args.jobs, args.redirect_stderr)
    else:
        devices = get_connected_device_list()
        if len(devices) == 0:
            print("You don't have any devices connected.")
        for device in devices:
            result = test_device(args.android_path, clang_version, device,
                                 args.jobs, args.clean_built_target,
                                 args.flashall_path, args.redirect_stderr)
            if not result and not args.keep_going:
                break


if __name__ == '__main__':
    main()