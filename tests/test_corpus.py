import io
import json
import socket
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests import TEST_PARENT
from corpus.acquisition import AcquisitionError, Downloader, challenge_page, pdf_link, public_addresses
from corpus.processing import Processor, WorkerLock, clean_whitespace, sections_to_chunks, working_directory
from corpus.storage import Library, digest
from corpus.tagging import infer_tags, normalize_tags
from corpus.get_files import submit


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}
    def getheader(self,name):
        return self.headers.get(name)


class Connection:
    def close(self):
        pass


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = working_directory(TEST_PARENT)
        self.root = self.temp.__enter__()
        self.library = Library(self.root/'data')
        self.processor = Processor(self.library,allowed_roots=[self.root])

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def import_text(self,collection='legal',body='Contract\n\nShared public authority.',title='Authority'):
        source = self.root/(uuid.uuid4().hex+'.txt')
        source.write_text(body,encoding='utf-8')
        job = self.library.enqueue(collection,{'path':str(source),'title':title,'expected_sha256':digest(source.read_bytes())})
        return self.processor.process(self.library.claim())

    def test_hash_dedup_and_collection_scoped_search(self):
        first = self.import_text()
        second = self.import_text('other')
        self.assertEqual(first['document_id'],second['document_id'])
        self.assertEqual(len(self.library.search('legal','contract')),1)
        self.assertEqual(self.library.search('missing','contract'),[])
        self.assertIsNone(self.library.document(first['document_id'],'missing'))
        with self.library.connect() as db:
            self.assertEqual(db.execute('select count(*) from documents').fetchone()[0],1)

    def test_subject_tags_and_combined_search(self):
        result = self.import_text(body='A semiconductor wafer fabrication process.\nPatent application for a new circuit.',title='Semiconductor research')
        doc = self.library.document(result['document_id'],'legal')
        self.assertIn('semiconductor-research',doc['tags'])
        self.assertIn('research',doc['tags'])
        found = self.library.search('legal','wafer tag:semiconductor-research',tags=['research'])
        self.assertEqual(found[0]['document_id'],doc['id'])
        self.assertEqual(self.library.search('legal','wafer',tags=['space-law']),[])
        self.assertEqual(len(self.library.search('legal','tag:semiconductor-research')),1)
        self.assertEqual(self.library.search('other','tag:semiconductor-research'),[])

    def test_caller_tags_survive_retag_and_stay_collection_scoped(self):
        first=self.import_text()
        doc_id=first['document_id']
        self.library.associate('legal',doc_id,{'source_id':'custom','tags':['Article Sources']})
        self.library.associate('other',doc_id,{'source_id':'other'})
        self.library.retag('legal',doc_id)
        self.library.associate('legal',doc_id,{'source_id':'custom','tags':['Future Research']})
        tags=self.library.document(doc_id,'legal')['tags']
        self.assertIn('article-sources',tags)
        self.assertIn('future-research',tags)
        self.assertNotIn('article-sources',self.library.document(doc_id,'other')['tags'])
        counts={row['tag']:row['documents'] for row in self.library.tag_inventory('legal')}
        self.assertEqual(counts['research'],1)

    def test_tagging_avoids_ambiguous_acronyms_and_incidental_mentions(self):
        tags=infer_tags('Patent appeal','CCPA opinion. '*10+'An example semiconductor citation.')
        self.assertNotIn('privacy-law',tags)
        self.assertNotIn('semiconductor-research',tags)
        self.assertIn('patent-law',tags)
        self.assertIn('privacy-law',infer_tags('California Consumer Privacy Act',''))
        self.assertIn('double-patenting',infer_tags('', 'double patent-\ning rule. '*4))

    def test_tag_validation_at_enqueue(self):
        self.assertEqual(normalize_tags(['Legal Research','legal_research']),['legal-research'])
        for tags in ['legal', [None], ['../bad'], ['x'*65]]:
            with self.assertRaises(ValueError):
                self.library.enqueue('legal',{'url':'https://example.com','tags':tags})

    def test_get_files_batch_download_only_preserves_bytes_without_indexing(self):
        body=b'%PDF-1.4\noriginal bytes without extractable text'
        self.processor.downloader=Downloader(self.library,transport=lambda url:(Connection(),Response(body,headers={'Content-Type':'application/pdf'})))
        batch=submit(self.library,{'items':['https://example.com/a.pdf',{'no_url':True},'https://example.com/b.pdf'],'mode':'download','format':'pdf','tags':['Legal Research']})
        self.assertIn('error',batch['items'][1])
        with patch.object(self.library,'throttle'):
            first=self.processor.process(self.library.claim())
            second=self.processor.process(self.library.claim())
        self.assertEqual(first['status'],'downloaded')
        self.assertEqual(Path(first['file']['path']).read_bytes(),body)
        self.assertEqual(first['file']['path'],second['file']['path'])
        self.assertEqual(first['input']['tags'],['legal-research'])
        with self.library.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_both_retains_original_when_normalizer_fails(self):
        body=b'%PDF-1.4\nnot a valid parseable PDF'
        self.processor.downloader=Downloader(self.library,transport=lambda url:(Connection(),Response(body,headers={'Content-Type':'application/pdf'})))
        submit(self.library,{'url':'https://example.com/a.pdf','mode':'both'})
        with patch.object(self.library,'throttle'),patch.object(self.processor,'normalize_file',side_effect=ValueError('cannot extract')):
            result=self.processor.process(self.library.claim())
        self.assertEqual(result['status'],'failed')
        self.assertEqual(Path(result['file']['path']).read_bytes(),body)

    def test_download_rejects_wrong_hash_and_fake_pdf(self):
        self.processor.downloader=Downloader(self.library,transport=lambda url:(Connection(),Response(b'plain text',headers={'Content-Type':'text/plain'})))
        submit(self.library,{'url':'https://example.com/a','mode':'download','format':'pdf'})
        with patch.object(self.library,'throttle'):
            result=self.processor.process(self.library.claim())
        self.assertEqual(result['error_kind'],'format_mismatch')
        self.assertNotIn('file',result)
        submit(self.library,{'items':[{'url':'https://example.com/b','expected_sha256':'0'*64}],'mode':'download'})
        with patch.object(self.library,'throttle'):
            result=self.processor.process(self.library.claim())
        self.assertEqual(result['error_kind'],'hash_mismatch')
        self.assertNotIn('file',result)

    def test_get_files_input_validation_and_idempotency(self):
        body={'urls':['https://example.com/a'],'mode':'download'}
        one=submit(self.library,body)
        self.assertEqual(one,submit(self.library,body))
        for invalid in [{'urls':[]},{'url':'https://example.com','urls':['https://example.com']},{'url':'https://example.com','mode':'bad'},{'url':'https://example.com','format':'exe'}]:
            with self.assertRaises(ValueError):submit(self.library,invalid)

    def test_tag_browse_can_find_scans_without_chunks(self):
        result=self.import_text(title='Manual of Patent Examining Procedure')
        with self.library.connect() as db:
            db.execute("UPDATE documents SET status='needs_ocr' WHERE id=?",(result['document_id'],))
            db.execute('DELETE FROM chunk_search WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)',(result['document_id'],))
            db.execute('DELETE FROM chunks WHERE document_id=?',(result['document_id'],))
        self.assertEqual(self.library.search('legal','tag:patent-law'),[])
        found=self.library.search('legal','tag:patent-law',include_incomplete=True)
        self.assertEqual(found[0]['status'],'needs_ocr')

    def test_whitespace_preserves_repeated_substantive_text_and_chunk_coverage(self):
        text=('Important substantive legal rule.\n\n'*700)+'Final rule.'
        clean = clean_whitespace(text)
        chunks = sections_to_chunks([{'text':clean,'locator':'page:2'}])
        self.assertTrue(all(len(c['text'])<=4000 for c in chunks))
        covered=set()
        for c in chunks:
            self.assertEqual(c['text'],clean[c['start_offset']:c['end_offset']])
            self.assertEqual(c['locator'],'page:2')
            covered.update(range(c['start_offset'],c['end_offset']))
        self.assertEqual(len(covered),len(clean))
        self.assertEqual(clean.count('Important'),700)

    def test_idempotency_and_refresh_preserve_versions(self):
        source=self.root/'sample.txt'
        source.write_text('first version')
        item={'path':str(source),'idempotency_key':'first'}
        one=self.library.enqueue('legal',item)
        self.assertEqual(one['id'],self.library.enqueue('legal',item)['id'])
        one=self.processor.process(self.library.claim())
        with self.assertRaises(ValueError):
            self.library.enqueue('legal',{**item,'title':'different request'})
        source.write_text('second version')
        self.library.enqueue('legal',{**item,'idempotency_key':'second'})
        two=self.processor.process(self.library.claim())
        self.assertNotEqual(one['document_id'],two['document_id'])
        self.assertIsNotNone(self.library.document(one['document_id'],'legal'))

    def test_wrong_hash_and_challenge_are_not_indexed(self):
        source=self.root/'bad.html'
        source.write_text('<title>Federal Register :: Request Access</title>Your request has been flagged as potentially automated')
        self.library.enqueue('legal',{'path':str(source)})
        self.assertEqual(self.processor.process(self.library.claim())['status'],'needs_access')
        self.library.enqueue('legal',{'path':str(source),'expected_sha256':'0'*64})
        self.assertEqual(self.processor.process(self.library.claim())['error_kind'],'hash_mismatch')
        self.assertEqual(self.library.search('legal','Register'),[])

    def test_private_addresses_and_redirects_rejected(self):
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:192.168.1.1','64:ff9b::7f00:1','64:ff9b::808:808','64:ff9b:1::1']:
            with self.assertRaises(AcquisitionError):
                public_addresses('test',443,lookup=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))])
        transport=lambda url:(Connection(),Response(b'',302,{'Location':'file:///etc/passwd'}))
        with patch.object(self.library,'throttle'):
            with self.assertRaises(AcquisitionError):
                Downloader(self.library,transport=transport).download('https://example.com/doc',self.root/'download')

    def test_streaming_size_limit_without_content_length(self):
        transport=lambda url:(Connection(),Response(b'A'*200,headers={'Content-Type':'text/plain'}))
        with patch.object(self.library,'throttle'):
            with self.assertRaises(AcquisitionError) as error:
                Downloader(self.library,max_bytes=100,transport=transport).download('https://example.com/doc',self.root/'download')
        self.assertEqual(error.exception.kind,'too_large')

    def test_retry_after_and_crash_recovery(self):
        transport=lambda url:(Connection(),Response(b'',429,{'Retry-After':'120'}))
        self.processor.downloader=Downloader(self.library,transport=transport)
        job=self.library.enqueue('legal',{'url':'https://example.com/doc'})
        with patch.object(self.library,'throttle'):
            result=self.processor.process(self.library.claim())
        self.assertEqual(result['status'],'retry_wait')
        self.assertEqual(result['history'][-1]['http_status'],429)
        self.assertGreaterEqual(result['available_at']-result['updated_at'],119)
        with self.library.connect() as db:
            db.execute("update jobs set status='working' where id=?",(job['id'],))
        with WorkerLock(self.library.root):
            self.library.recover()
        self.assertEqual(self.library.job(job['id'])['status'],'queued')

    def test_alternate_url_succeeds_and_records_both_attempts(self):
        def transport(url):
            return Connection(),Response(b'Access refused',403) if url.endswith('first') else Response(b'The complete alternate source.',headers={'Content-Type':'text/plain'})
        self.processor.downloader=Downloader(self.library,transport=transport)
        self.library.enqueue('legal',{'url':'https://example.com/first','alternate_urls':['https://example.com/second']})
        with patch.object(self.library,'throttle'):
            result=self.processor.process(self.library.claim())
        self.assertEqual(result['status'],'ready')
        self.assertEqual([a['outcome'] for a in result['history']],['needs_access','accepted'])

    def test_unique_pdf_landing_link_and_ambiguity(self):
        self.assertEqual(pdf_link(b'<meta name="citation_pdf_url" content="/article.pdf">','https://example.com/paper'),'https://example.com/article.pdf')
        with self.assertRaises(AcquisitionError):
            pdf_link(b'<a href="a.pdf">A</a><a href="b.pdf">B</a>','https://example.com/')

    def test_pdf_keeps_rotated_text_and_flags_blank_page(self):
        try:
            from reportlab.pdfgen import canvas
        except ImportError:
            self.skipTest('reportlab is a test-only package')
        source=self.root/'rotated.pdf'
        pdf=canvas.Canvas(str(source))
        pdf.drawString(70,700,'Normal legal clause')
        pdf.saveState(); pdf.translate(150,200); pdf.rotate(90); pdf.drawString(0,0,'Rotated legal clause'); pdf.restoreState()
        pdf.showPage(); pdf.showPage(); pdf.save()
        self.library.enqueue('legal',{'path':str(source)})
        result=self.processor.process(self.library.claim())
        self.assertEqual(result['status'],'needs_ocr')
        self.assertEqual(self.library.search('legal','Rotated'),[])
        found=self.library.search('legal','Rotated',include_incomplete=True)
        self.assertIn('Rotated legal clause',found[0]['text'])
        self.assertEqual(found[0]['locator'],'page:1')


if __name__=='__main__':
    unittest.main()
