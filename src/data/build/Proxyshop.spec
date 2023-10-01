# -*- mode: python ; coding: utf-8 -*-
# Standard Library Imports
from os import path as osp

# Third Party Imports
from kivy_deps import sdl2, glew

# Local Imports
from src.constants import con
block_cipher = None


a = Analysis([osp.join(con.cwd, 'main.py')],
             binaries=[],
             datas=[],
             hiddenimports=[
                'src.templates',
                'src.gui',
                'kivy',
                'svglib.svglib',
                'reportlab.graphics',
                'requests'
             ],
             hookspath=[],
             hooksconfig={},
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)

pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)

exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,  
          *[Tree(p) for p in (sdl2.dep_bins + glew.dep_bins)],
          name='Proxyshop',
          debug=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=False,
          disable_windowed_traceback=False,
          target_arch=None,
          codesign_identity=None,
          icon=osp.join(con.path_img, 'favicon.ico'))