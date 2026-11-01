import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from space_tools import own_path, call


class WorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'space'
        self.root.mkdir()
        self.outside=Path(self.tmp.name)/'outside'
        self.outside.mkdir()
        self.links=[]
        self.store=SimpleNamespace(root=self.root,identity=SimpleNamespace(actor=lambda:'biao'),check_storage=lambda:None)

    def tearDown(self):
        for link in reversed(self.links):
            if os.name=='nt':os.rmdir(link)
            else:link.unlink()
        self.tmp.cleanup()

    def link(self,path,target):
        if os.name=='nt':
            # Targets are fixed temporary fixture paths, no user shell interpolation.
            script='New-Item -ItemType Junction -Path $env:DSH_TEST_LINK -Target $env:DSH_TEST_TARGET | Out-Null'
            env=dict(os.environ,DSH_TEST_LINK=str(path),DSH_TEST_TARGET=str(target))
            subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],env=env,check=True,capture_output=True)
        else:path.symlink_to(target,target_is_directory=True)
        self.links.append(path)

    def test_workdir_parent_junction_rejected_before_creating_actor(self):
        self.link(self.root/'workdir',self.outside)
        with self.assertRaises(ValueError):own_path(self.store,'note.txt',True)
        self.assertEqual(list(self.outside.iterdir()),[])

    def test_space_root_junction_rejected(self):
        alias=Path(self.tmp.name)/'alias'
        self.link(alias,self.root)
        self.store.root=alias
        with self.assertRaises(ValueError):own_path(self.store,'note.txt',True)
        self.assertFalse((self.root/'workdir').exists())

    def test_version_history_junction_cannot_copy_private_content_out(self):
        base,p=own_path(self.store,'note.txt',True)
        p.write_text('keep original',encoding='utf-8')
        self.link(base/'.versions',self.outside)
        with self.assertRaises(ValueError):call(self.store,'workspace_write',{'path':'note.txt','text':'changed'})
        self.assertEqual(p.read_text('utf-8'),'keep original')
        self.assertEqual(list(self.outside.iterdir()),[])

    def test_normal_write_version_and_read_still_work(self):
        call(self.store,'workspace_write',{'path':'notes/hello.txt','text':'first'})
        call(self.store,'workspace_write',{'path':'notes/hello.txt','text':'second'})
        self.assertEqual(call(self.store,'workspace_read',{'path':'notes/hello.txt'})['text'],'second')
        versions=list((self.root/'workdir/biao/.versions').iterdir())
        self.assertEqual(len(versions),1)
        self.assertEqual(versions[0].read_text('utf-8'),'first')


if __name__=='__main__':unittest.main()
