import secrets
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import TEST_PARENT
from corpus.backup import create_backup,restore_backup,verify_data
from corpus.processing import Processor,WorkerLock,working_directory
from corpus.storage import Library
from corpus.protected_key import load_key
from corpus.publication import PublicationError


class BackupTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32','DPAPI is only available on Windows')
    def test_dpapi_key_round_trip_without_plaintext_file(self):
        with working_directory(TEST_PARENT) as root:
            key_file=root/'fixture.dpapi';key=load_key(key_file,create=True)
            self.assertEqual(len(key),32);self.assertNotIn(key,key_file.read_bytes())
            self.assertEqual(load_key(key_file),key)

    def test_backup_restore_preserves_artifacts_and_rejects_tampering(self):
        with working_directory(TEST_PARENT) as root:
            library=Library(root/'live');processor=Processor(library,allowed_roots=[root])
            source=root/'fixture.txt';source.write_text('Semiconductor patent fixture for backup.')
            library.enqueue('research',{'path':str(source)});job=processor.process(library.claim())
            self.assertEqual(job['status'],'ready')
            # Runtime credentials must never enter a backup package.
            (library.root/'service-token.txt').write_text('fixture-token-not-backed-up')
            key=secrets.token_bytes(32);backup=root/'backups'/'one.scbk'
            with WorkerLock(library.root):
                with self.assertRaises(RuntimeError):create_backup(library.root,backup,key)
            report=create_backup(library.root,backup,key)
            self.assertEqual(report['integrity'],'ok')
            restored=root/'restored';restore_backup(backup,restored,key)
            self.assertFalse((restored/'service-token.txt').exists())
            self.assertEqual(verify_data(restored)['counts'],verify_data(library.root)['counts'])
            restored_library=Library(restored)
            self.assertEqual(restored_library.search('research','semiconductor')[0]['document_id'],job['document_id'])
            document=restored_library.document(job['document_id'],'research')
            self.assertEqual(restored_library.artifact_path(document['original_key']).read_text(),source.read_text())
            with self.assertRaises(ValueError):restore_backup(backup,restored,key)
            corrupted=root/'corrupt.scbk';payload=bytearray(backup.read_bytes());payload[-3]^=1;corrupted.write_bytes(payload)
            for bad in [corrupted]:
                with self.assertRaises(Exception):restore_backup(bad,root/'must-not-exist',key)
                self.assertFalse((root/'must-not-exist').exists())
            truncated=root/'truncated.scbk';truncated.write_bytes(backup.read_bytes()[:-20])
            with self.assertRaises(Exception):restore_backup(truncated,root/'must-not-exist',key)
            with self.assertRaises(Exception):restore_backup(backup,root/'must-not-exist',secrets.token_bytes(32))

    def test_restore_ignores_outside_journal_paths_and_rebases_downloads(self):
        with working_directory(TEST_PARENT) as root:
            library=Library(root/'live');processor=Processor(library,allowed_roots=[root])
            source=root/'fixture.txt';source.write_text('Safe restore fixture.')
            job=library.enqueue('research',{'path':str(source),'mode':'both'})
            receipt=processor.process(library.claim())
            outside=root/'outside';outside.mkdir()
            with library.connect() as db:
                db.execute('UPDATE publication_journal SET candidate_dir=?,target_dir=?',(str(outside),str(outside)))
                db.execute('UPDATE download_publication_journal SET candidate_path=?,target_path=?',(str(outside/'candidate'),str(outside/'target')))
            key=secrets.token_bytes(32);backup=root/'rebased.scbk';create_backup(library.root,backup,key)
            restored=root/'restored';restore_backup(backup,restored,key)
            restored_library=Library(restored)
            self.assertEqual(restored_library.document(receipt['document_id'],'research')['id'],receipt['document_id'])
            restored_receipt=restored_library.job(job['id'])
            self.assertEqual(Path(restored_receipt['file']['path']).read_bytes(),source.read_bytes())
            with restored_library.connect() as db:
                document_target=Path(db.execute('SELECT target_dir FROM publication_journal').fetchone()[0]).resolve()
                download_target=Path(db.execute('SELECT target_path FROM download_publication_journal').fetchone()[0]).resolve()
            self.assertTrue(document_target.is_relative_to(restored.resolve()))
            self.assertTrue(download_target.is_relative_to(restored.resolve()))
            self.assertFalse(any(outside.iterdir()))

    def test_outside_selected_path_quarantines_without_secondary_error(self):
        with working_directory(TEST_PARENT) as root:
            library=Library(root/'live');processor=Processor(library,allowed_roots=[root])
            source=root/'fixture.txt';source.write_text('Quarantine fixture.')
            library.enqueue('research',{'path':str(source)});job=processor.process(library.claim())
            with library.connect() as db:
                db.execute('UPDATE publication_journal SET target_dir=?',(str(root/'outside'),))
            with self.assertRaises(PublicationError):library.document(job['document_id'],'research')
            with library.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM documents WHERE id=?',(job['document_id'],)).fetchone()[0],
                                 'reconciliation_required')

    def test_restore_rejects_inconsistent_download_receipt_without_destination(self):
        for field,value in [('bytes',999),('retrieval_json','{"final_url":"https://attacker.invalid/"}')]:
            with self.subTest(field=field),working_directory(TEST_PARENT) as root:
                library=Library(root/'live');processor=Processor(library,allowed_roots=[root])
                source=root/'fixture.txt';source.write_text('Receipt binding fixture.')
                library.enqueue('research',{'path':str(source),'mode':'download'})
                processor.process(library.claim())
                with library.connect() as db:
                    db.execute(f'UPDATE file_artifacts SET {field}=?',(value,))
                key=secrets.token_bytes(32);backup=root/'inconsistent.scbk'
                # Build an authenticated fixture whose artifact files remain intact but whose
                # database receipt disagrees with its publication journal.
                with patch('corpus.backup.rebase_publication_journals',return_value=None):
                    create_backup(library.root,backup,key)
                destination=root/'must-not-exist'
                with self.assertRaises(ValueError):restore_backup(backup,destination,key)
                self.assertFalse(destination.exists())

    def test_restore_legacy_backup_without_journal_tables(self):
        with working_directory(TEST_PARENT) as root:
            library=Library(root/'live');processor=Processor(library,allowed_roots=[root])
            source=root/'fixture.txt';source.write_text('Legacy semiconductor journal fixture.')
            library.enqueue('research',{'path':str(source)});job=processor.process(library.claim())
            with library.connect() as db:
                db.execute('DROP TABLE publication_job_completions')
                db.execute('DROP TABLE publication_journal')
                db.execute('DROP TABLE download_publication_journal')
            key=secrets.token_bytes(32);backup=root/'legacy.scbk';create_backup(library.root,backup,key)
            restored=root/'restored';restore_backup(backup,restored,key)
            self.assertTrue(restored.is_dir())
            restored_library=Library(restored)
            result=restored_library.search('research','semiconductor')[0]
            self.assertEqual(result['document_id'],job['document_id'])
            with restored_library.connect() as db:
                self.assertEqual(db.execute('SELECT state FROM publication_journal').fetchone()[0],'PUBLISHED')

    def test_restore_legacy_backup_without_download_journal(self):
        with working_directory(TEST_PARENT) as root:
            library=Library(root/'live');processor=Processor(library,allowed_roots=[root])
            source=root/'fixture.txt';source.write_text('Legacy mixed journal fixture.')
            submitted=library.enqueue('research',{'path':str(source),'mode':'both'})
            job=processor.process(library.claim())
            outside=root/'outside';outside.mkdir()
            with library.connect() as db:
                db.execute('UPDATE publication_journal SET candidate_dir=?,target_dir=?',(str(outside),str(outside)))
                db.execute('DROP TABLE download_publication_journal')
            key=secrets.token_bytes(32);backup=root/'legacy-mixed.scbk';create_backup(library.root,backup,key)
            restored=root/'restored';restore_backup(backup,restored,key)
            self.assertTrue(restored.is_dir())
            restored_library=Library(restored)
            self.assertEqual(restored_library.search('research','mixed')[0]['document_id'],job['document_id'])
            receipt=restored_library.job(submitted['id'])
            self.assertEqual(Path(receipt['file']['path']).read_bytes(),source.read_bytes())
            with restored_library.connect() as db:
                target=Path(db.execute('SELECT target_dir FROM publication_journal').fetchone()[0]).resolve()
                self.assertTrue(target.is_relative_to(restored.resolve()))
                self.assertEqual(db.execute('SELECT state FROM download_publication_journal').fetchone()[0],'PUBLISHED')
            self.assertFalse(any(outside.iterdir()))


if __name__=='__main__':unittest.main()
