# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng

import romfs


def test_version_is_string():
    assert isinstance(romfs.__version__, str)
    assert romfs.__version__.count(".") >= 1
