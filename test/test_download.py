#!/usr/bin/env python

from __future__ import unicode_literals

# Allow direct execution
import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.helper import (
    assertGreaterEqual,
    expect_warnings,
    get_params,
    gettestcases,
    expect_info_dict,
    try_rm,
    report_warning,
)


import hashlib
import io
import json
import socket

import youtube_dl.YoutubeDL
from youtube_dl.compat import (
    compat_http_client,
    compat_urllib_error,
    compat_HTTPError,
)
from youtube_dl.utils import (
    DownloadError,
    ExtractorError,
    format_bytes,
    UnavailableVideoError,
)
from youtube_dl.extractor import get_info_extractor

RETRIES = 3


class YoutubeDL(youtube_dl.YoutubeDL):
    def __init__(self, *args, **kwargs):
        self.to_stderr = self.to_screen
        self.processed_info_dicts = []
        super(YoutubeDL, self).__init__(*args, **kwargs)

    def report_warning(self, message):
        # Don't accept warnings during tests
        raise ExtractorError(message)

    def process_info(self, info_dict):
        self.processed_info_dicts.append(info_dict)
        return super(YoutubeDL, self).process_info(info_dict)


def _file_md5(fn):
    with open(fn, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()

defs = gettestcases()


class TestDownload(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.defs = defs

# Dynamically generate tests


def generator(test_case):

    def test_template(self):
        ie = youtube_dl.extractor.get_info_extractor(test_case['name'])
        other_ies = [get_info_extractor(ie_key) for ie_key in test_case.get('add_ie', [])]
        is_playlist = any(k.startswith('playlist') for k in test_case)
        test_cases = test_case.get(
            'playlist', [] if is_playlist else [test_case])

        def print_skipping(reason):
            print('Skipping %s: %s' % (test_case['name'], reason))
        if not ie.working():
            print_skipping('IE marked as not _WORKING')
            return

        for tc in test_cases:
            info_dict = tc.get('info_dict', {})
            if not tc.get('file') and not (info_dict.get('id') and info_dict.get('ext')):
                raise Exception('Test definition incorrect. The output file cannot be known. Are both \'id\' and \'ext\' keys present?')

        if 'skip' in test_case:
            print_skipping(test_case['skip'])
            return
        for other_ie in other_ies:
            if not other_ie.working():
                print_skipping('test depends on %sIE, marked as not WORKING' % other_ie.ie_key())
                return

        params = get_params(test_case.get('params', {}))
        if is_playlist and 'playlist' not in test_case:
            params.setdefault('extract_flat', True)
            params.setdefault('skip_download', True)

        ydl = YoutubeDL(params, auto_init=False)
        ydl.add_default_info_extractors()
        finished_hook_called = set()

        def _hook(status):
            if status['status'] == 'finished':
                finished_hook_called.add(status['filename'])
        ydl.add_progress_hook(_hook)
        expect_warnings(ydl, test_case.get('expected_warnings', []))

        def get_tc_filename(tc):
            return tc.get('file') or ydl.prepare_filename(tc.get('info_dict', {}))

        res_dict = None

        def try_rm_tcs_files(tcs=None):
            if tcs is None:
                tcs = test_cases
            for tc in tcs:
                tc_filename = get_tc_filename(tc)
                try_rm(tc_filename)
                try_rm(tc_filename + '.part')
                try_rm(os.path.splitext(tc_filename)[0] + '.info.json')
        try_rm_tcs_files()
        try:
            try_num = 1
            while True:
                try:
                    # We're not using .download here sine that is just a shim
                    # for outside error handling, and returns the exit code
                    # instead of the result dict.
                    res_dict = ydl.extract_info(test_case['url'])
                except (DownloadError, ExtractorError) as err:
                    # Check if the exception is not a network related one
                    if not err.exc_info[0] in (compat_urllib_error.URLError, socket.timeout, UnavailableVideoError, compat_http_client.BadStatusLine) or (err.exc_info[0] == compat_HTTPError and err.exc_info[1].code == 503):
                        raise

                    if try_num == RETRIES:
                        report_warning('Failed due to network errors, skipping...')
                        return

                    print('Retrying: {0} failed tries\n\n##########\n\n'.format(try_num))

                    try_num += 1
                else:
                    break

            if is_playlist:
                self.assertEqual(res_dict['_type'], 'playlist')
                self.assertTrue('entries' in res_dict)
                expect_info_dict(self, res_dict, test_case.get('info_dict', {}))

            if 'playlist_mincount' in test_case:
                assertGreaterEqual(
                    self,
                    len(res_dict['entries']),
                    test_case['playlist_mincount'],
                    'Expected at least %d in playlist %s, but got only %d' % (
                        test_case['playlist_mincount'], test_case['url'],
                        len(res_dict['entries'])))
            if 'playlist_count' in test_case:
                self.assertEqual(
                    len(res_dict['entries']),
                    test_case['playlist_count'],
                    'Expected %d entries in playlist %s, but got %d.' % (
                        test_case['playlist_count'],
                        test_case['url'],
                        len(res_dict['entries']),
                    ))
            if 'playlist_duration_sum' in test_case:
                got_duration = sum(e['duration'] for e in res_dict['entries'])
                self.assertEqual(
                    test_case['playlist_duration_sum'], got_duration)

            for tc in test_cases:
                tc_filename = get_tc_filename(tc)
                if not test_case.get('params', {}).get('skip_download', False):
                    self.assertTrue(os.path.exists(tc_filename), msg='Missing file ' + tc_filename)
                    self.assertTrue(tc_filename in finished_hook_called)
                    expected_minsize = tc.get('file_minsize', 10000)
                    if expected_minsize is not None:
                        if params.get('test'):
                            expected_minsize = max(expected_minsize, 10000)
                        got_fsize = os.path.getsize(tc_filename)
                        assertGreaterEqual(
                            self, got_fsize, expected_minsize,
                            'Expected %s to be at least %s, but it\'s only %s ' %
                            (tc_filename, format_bytes(expected_minsize),
                                format_bytes(got_fsize)))
                    if 'md5' in tc:
                        md5_for_file = _file_md5(tc_filename)
                        self.assertEqual(md5_for_file, tc['md5'])
                info_json_fn = os.path.splitext(tc_filename)[0] + '.info.json'
                self.assertTrue(
                    os.path.exists(info_json_fn),
                    'Missing info file %s' % info_json_fn)
                with io.open(info_json_fn, encoding='utf-8') as infof:
                    info_dict = json.load(infof)

                expect_info_dict(self, info_dict, tc.get('info_dict', {}))
        finally:
            try_rm_tcs_files()
            if is_playlist and res_dict is not None and res_dict.get('entries'):
                # Remove all other files that may have been extracted if the
                # extractor returns full results even with extract_flat
                res_tcs = [{'info_dict': e} for e in res_dict['entries']]
                try_rm_tcs_files(res_tcs)

    return test_template

# And add them to TestDownload
for n, test_case in enumerate(defs):
    test_method = generator(test_case)
    tname = 'test_' + str(test_case['name'])
    i = 1
    while hasattr(TestDownload, tname):
        tname = 'test_%s_%d' % (test_case['name'], i)
        i += 1
    test_method.__name__ = str(tname)
    setattr(TestDownload, test_method.__name__, test_method)
    del test_method


if __name__ == '__main__':
    unittest.main()
