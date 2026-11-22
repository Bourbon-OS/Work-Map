"""Check a disposable copy; never opens the user's data/workmap.sqlite3."""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def storage_shape(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        return (db.execute('PRAGMA application_id').fetchone()[0],
                db.execute('PRAGMA user_version').fetchone()[0],
                db.execute('SELECT count(*) FROM snapshots').fetchone()[0])


def copy_pin_guide(target):
    shutil.copyfile(ROOT / '_workmap_runtime.py', target / '_workmap_runtime.py')
    shutil.copyfile(ROOT / 'BOOTSTRAP.json', target / 'BOOTSTRAP.json')
    shutil.copytree(ROOT / '.workmap-payload', target / '.workmap-payload')
    # This is the only bundled document required by the Pin binding. Never
    # copy a used kit's docs tree: it can contain private Owner evidence.
    relative = Path('docs/work-management/PIN_ROUTINE_ENTRY_2026-10-02.md')
    dest = target / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / relative, dest)


def scratch_directory(prefix):
    path = Path(tempfile.gettempdir()).resolve()
    # Keep copy/cleanup working for versioned evidence in a deeply extracted kit.
    if os.name == 'nt' and not str(path).startswith('\\\\'):
        path = Path('\\\\?\\' + str(path))
    return tempfile.TemporaryDirectory(prefix=prefix, dir=path)


class PersonalStarterCheck(unittest.TestCase):
    def test_isolated_lifecycle(self):
        with scratch_directory('wm-') as temp:
            target = Path(temp.removeprefix('\\\\?\\')) / 'kit'
            target.mkdir()
            for name in ('src', 'tools'):
                shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT / 'workmap.py', target / 'workmap.py')
            shutil.copyfile(ROOT / 'WorkMap.pyw', target / 'WorkMap.pyw')
            copy_pin_guide(target)
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
            env.pop('PYTHONPATH', None)
            def run(*args, success=True):
                r = subprocess.run([sys.executable, '-I', '-X', 'utf8', 'workmap.py', *args], cwd=target,
                                   env=env, text=True, encoding='utf-8', capture_output=True)
                self.assertEqual(r.returncode == 0, success, r.stdout + r.stderr)
                return r.stdout
            common = ['--name', '合成試験', '--intent', '判断理由を保つ', '--origin', '隔離確認', '--owner-instruction', 'この合成Workを登録する']
            db = target / 'data/workmap.sqlite3'
            first = json.loads(run('init', *common))
            self.assertEqual(first['display_id'], 'W0')
            self.assertEqual(storage_shape(db), (0x574D5354, 1, 1))
            self.assertIn('新形式のMap', run('show'))
            before = db.read_bytes()
            run('init', *common, success=False)
            self.assertEqual(db.read_bytes(), before)

            added = json.loads(run('add', '--parent', 'W0', *common))
            self.assertEqual(added['status'], 'confirmed')
            self.assertEqual(json.loads(run('thread', '--work', 'W1'))['thread_entries'], [])
            run('add', '--parent', 'W404', *common, success=False)
            run('add', '--parent', 'W1', *common, success=False)
            record_args = ['--text', '電子版と紙版の両方を比較する', '--owner-instruction', 'この判断理由を記録する']
            permit = ['permit', '--work', 'W1', '--edit', '--record', '--close', '--pin',
                      '--owner-instruction', 'W1の指定操作をOwnerに許可する']
            pin = ['pin', '--work', 'W1', '--reason', '比較を担当する', '--owner-instruction', 'W1を引き受ける']
            run('reason', '--work', 'W1', *record_args, success=False)
            run(*pin, success=False)
            before = db.read_bytes()
            run(*permit, '--actor', 'actor:synthetic-other', success=False)
            self.assertEqual(db.read_bytes(), before)
            run(*permit)
            run('reason', '--work', 'W1', *record_args, success=False)
            run(*pin)
            run('reason', '--work', 'W1', *record_args)
            detail = json.loads(run('resume', '--work', 'W1'))
            self.assertEqual(detail['past_result'], 'unknown')
            self.assertEqual(detail['current']['meaning'][0]['why'], record_args[1])
            run('history', '--work', 'W1', success=False)

            run('permit', '--work', 'W0', '--map-add', '--owner-instruction', 'このMapへの登録を許可する')
            run('permit', '--work', 'W1', '--map-add', '--owner-instruction', 'W1はMap全体ではない', success=False)
            run('add', '--parent', 'W1', *common)
            run('permit', '--work', 'W1.1', '--pin', '--close', '--owner-instruction', 'W1.1の引受と終了を許可する')
            run('pin', '--work', 'W1.1', '--reason', '確認へ切り替える', '--owner-instruction', 'W1.1を引き受ける')
            run('close', '--work', 'W1.1', '--outcome', 'stopped', '--reason', '確認済みだった',
                '--owner-instruction', 'この確認を打ち切る', '--release-own-pin')
            run(*pin)
            proof = target / 'docs/work-management/evidence/proof.txt'
            proof.write_text('紙版の継続と電子版の試行を比較した。', encoding='utf-8')
            ref = 'repo:docs/work-management/evidence/proof.txt'
            run('result', '--work', 'W1', '--text', '比較を完了', '--source', ref)
            close = ['close', '--work', 'W1', '--outcome', 'completed', '--reason', '比較を終えた',
                     '--owner-instruction', '比較を完了として閉じる', '--release-own-pin']
            run(*close, success=False)
            run(*close, '--evidence', ref)
            closed = json.loads(run('thread', '--work', added['work_key']))['thread_entries']
            self.assertTrue(any(e.get('outcome') == 'completed' for e in closed))
            self.assertTrue(any(e.get('why') == record_args[1] for e in closed))
            self.assertEqual(storage_shape(db), (0x574D5354, 1, 2))

            backup_dir = target / 'backups'
            backup_dir.mkdir()
            backup = backup_dir / 'backup.json'
            saved = json.loads(run('backup', '--output', str(backup)))
            candidate = backup_dir / 'candidate.sqlite3'
            check = ['recover-check', '--backup', str(backup), '--sha256', saved['sha256'], '--target']
            original = db.read_bytes()
            run(*check, str(db), success=False)
            self.assertEqual(db.read_bytes(), original)
            restored = json.loads(run(*check, str(candidate)))
            self.assertEqual(restored['store_role'], 'recovery_candidate')
            self.assertEqual(storage_shape(candidate), (0x574D5354, 1, 1))
            view = json.loads(run('recovery-show', '--candidate', str(candidate), '--work', added['work_key'], '--thread'))
            self.assertTrue(view['read_only'])
            self.assertEqual(view['thread_entries'], closed)
            self.assertEqual(db.read_bytes(), original)

            def restore(*, project=view['project_id'], token=view['candidate_token'],
                        hash_value=saved['sha256'], success=True):
                return run('restore', '--backup', str(backup), '--candidate', str(candidate),
                           '--sha256', hash_value, '--candidate-token', token, '--project-id', project,
                           '--owner-instruction', 'この候補の内容・担当・許可を照合し、保全した元の場所へ復旧する',
                           success=success)

            restore(success=False)
            self.assertEqual(db.read_bytes(), original)
            preserved = db.with_name('preserved.sqlite3')
            db.rename(preserved)
            candidate_after_loss = backup_dir / 'loss-check.sqlite3'
            run(*check, str(candidate_after_loss))
            restore(project='project:other', success=False)
            restore(token='different-content', success=False)
            restore(hash_value='0' * 64, success=False)
            self.assertFalse(db.exists())
            sidecar = db.with_name(db.name + '-journal')
            sidecar.write_bytes(b'existing sidecar')
            restore(success=False)
            self.assertEqual(sidecar.read_bytes(), b'existing sidecar')
            sidecar.unlink()

            evidence_dir = candidate.with_name(candidate.name + '.evidence')
            changed = next(evidence_dir.glob('*.source'))
            content = changed.read_bytes()
            changed.write_bytes(b'changed evidence')
            restore(success=False)
            self.assertFalse(db.exists())
            changed.write_bytes(content)

            reopened = json.loads(restore())
            self.assertEqual(reopened['store_role'], 'canonical_current')
            self.assertNotEqual(reopened['revision'], view['revision'])
            self.assertEqual(json.loads(run('thread', '--work', added['work_key']))['thread_entries'], closed)
            self.assertEqual(preserved.read_bytes(), original)
            self.assertEqual(json.loads(run('add', '--parent', 'W0', *common))['status'], 'confirmed')

    def test_actual_gui_launcher_without_development_context(self):
        with scratch_directory('wmg-') as temp:
            target = Path(temp.removeprefix('\\\\?\\')) / 'kit'; target.mkdir()
            for name in ('src', 'tools'):
                shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('workmap.py', 'WorkMap.pyw'):
                shutil.copyfile(ROOT / name, target / name)
            copy_pin_guide(target)
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
            env.pop('PYTHONPATH', None)
            for args in (
                ['init', '--name', 'GUI隔離確認', '--intent', '空の環境で表示できる', '--origin', 'GUI起動試験', '--owner-instruction', 'このMapを作る'],
                ['permit', '--work', 'W0', '--map-add', '--owner-instruction', 'このMapへのOwnerのWork追加を許可する'],
                ['permit', '--work', 'W0', '--edit', '--owner-instruction', 'W0のOwner編集を許可する'],
            ):
                result = subprocess.run([sys.executable, '-I', '-X', 'utf8', 'workmap.py', *args], cwd=target, env=env, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            probe = target / 'gui_probe.py'
            probe.write_text('''import json, runpy, sys, time, traceback
from pathlib import Path
from unittest.mock import patch
base=Path(__file__).resolve().parent
relocated=len(sys.argv)>1
missing=len(sys.argv)>1 and sys.argv[1]=='missing'
sys.path[:0]=[str(base/'src'),str(base)]
import tkinter as tk
import pma.workmap_viewer as viewer
original_viewer=viewer.WorkMapViewer
original_loop=tk.Tk.mainloop
instances=[]; errors=[]; checked=[]
class CapturedViewer(original_viewer):
    def __init__(self,*args,**kw):
        super().__init__(*args,**kw); instances.append(self)
viewer.WorkMapViewer=CapturedViewer
def loop(root,*args,**kw):
    root.withdraw(); deadline=time.monotonic()+10
    def check():
        app=instances[0]
        if not missing and app.data is None and time.monotonic()<deadline:
            root.after(20,check); return
        try:
            if missing:
                assert app.data is None and not app.sources, 'missing own DB opened an old session Map'
                assert not app.session_writable, 'missing DB overwrote the saved connections'
                checked.append(True)
                return
            assert app.data is not None, 'Map did not load'
            assert len(app.sources)==1,[(str(s.path),s.project_id) for s in app.sources]
            assert app.sources[0].path==(base/'data/workmap.sqlite3').resolve()
            assert app.sources[0].evidence_root==base
            assert app.operator_id=='actor:local-owner'
            assert app.sources[0].state_trial
            assert app.task_index_path is None
            assert app.tree.get_children(), 'Map tree is empty'
            assert app.app_icon.width()>0
            assert app.add_button.instate(['!disabled']), 'Map registration form is unavailable'
            app.auto_button.invoke(); assert app.auto_enabled.get()
            app.auto_button.invoke(); assert not app.auto_enabled.get()
            from pma.workmap_editor_ui import EditDialog
            evidence=base/'docs/work-management/evidence/owner_gui_trial.txt'
            evidence.write_text('OwnerはGUIで今回の名前を訂正する。目的は変更しない。',encoding='utf-8')
            key=app.data['snapshot']['aliases']['W0']
            dialog=EditDialog(app,key)
            dialog.new_text.delete('1.0','end')
            dialog.new_text.insert('1.0','複製先のGUI確認' if relocated else 'GUIで訂正した名前')
            dialog.reason_input.set('名前を分かりやすく訂正する')
            with patch('pma.workmap_editor_ui.filedialog.askopenfilenames',return_value=[str(evidence)]), patch('pma.workmap_editor_ui.messagebox.askyesno',return_value=True):
                dialog.choose_button.invoke(); dialog.preview_button.invoke()
                assert dialog.prepared is not None,dialog.message.get()
                dialog.save_button.invoke()
                if relocated:
                    assert dialog.last_result['status']=='rejected',dialog.last_result
                else:
                    assert dialog.last_result['status']=='confirmed',dialog.last_result
            dialog.destroy()
            if not relocated:
                deadline=time.monotonic()+5
                while app.data is None and time.monotonic()<deadline:
                    root.update()
                    time.sleep(0.02)
                assert app.data is not None, 'Map did not refresh after editing'
                from pma.workmap_add_ui import AddWorkDialog
                add=AddWorkDialog(app,key)
                add.reload_snapshot()
                for field,value in ((add.name_text,'画面で追加した仕事'),(add.intent_text,'操作を確かめる'),
                                    (add.origin_text,'初期設定後の画面確認')):
                    field.insert('1.0',value)
                with patch('pma.workmap_editor_ui.filedialog.askopenfilenames',return_value=[str(evidence)]), patch('pma.workmap_add_ui.messagebox.askyesno',return_value=True):
                    add.choose_button.invoke(); add.preview_button.invoke()
                    assert add.prepared is not None,add.message.get()
                    add.save_button.invoke()
                    assert add.last_result and add.last_result['status']=='confirmed',add.message.get()
                add.destroy()
            checked.append(True)
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            app.close_window()
    root.after(20,check); return original_loop(root,*args,**kw)
tk.Tk.mainloop=loop
try:
    runpy.run_path(str(base/'WorkMap.pyw'),run_name='__main__')
except SystemExit as exc:
    assert exc.code==0, exc.code
assert checked and not errors, errors
assert (base/'data/viewer-session.json').is_file()
print(json.dumps({'gui':'started','own_map_only':True,'auto_on_off':True,'no_codex_dependency':True}))
''', encoding='utf-8')
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(probe)], cwd=target, env=env,
                                    text=True, encoding='utf-8', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['gui'], 'started')
            # Copy only this synthetic used kit, never the user's ROOT/data.
            original_bytes = (target / 'data/workmap.sqlite3').read_bytes()
            relocated = target.parent / 'moved'
            shutil.copytree(Path(temp) / target.name, Path(temp) / relocated.name,
                            ignore=shutil.ignore_patterns('__pycache__'))
            result = subprocess.run([sys.executable,'-I','-X','utf8',str(relocated/'gui_probe.py'),'relocated'],
                                    cwd=relocated,env=env,text=True,encoding='utf-8',capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual((target/'data/workmap.sqlite3').read_bytes(),original_bytes)
            missing = target.parent / 'missing-own-db'
            shutil.copytree(Path(temp) / target.name, Path(temp) / missing.name,
                            ignore=shutil.ignore_patterns('__pycache__'))
            (missing/'data/workmap.sqlite3').rename(missing/'data/preserved.sqlite3')
            session_before = (missing/'data/viewer-session.json').read_bytes()
            result = subprocess.run([sys.executable,'-I','-X','utf8',str(missing/'gui_probe.py'),'missing'],
                                    cwd=missing,env=env,text=True,encoding='utf-8',capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual((missing/'data/viewer-session.json').read_bytes(),session_before)
            self.assertEqual((target/'data/workmap.sqlite3').read_bytes(),original_bytes)


if __name__ == '__main__':
    unittest.main()
