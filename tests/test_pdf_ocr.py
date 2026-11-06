"""Synthetic OCR integration, isolated from production credentials and storage."""
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import TEST_PARENT
from corpus.processing import Processor, extract_source, working_directory, PDF_NORMALIZER_VERSION
from corpus.storage import Library
from corpus.vendor.pdf_ocr import apply_pdf_ocr
from corpus.vendor.context_forge_extractors import Extraction, Section


class CorpusOcrTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('GET_FILES_CONTEXT_FORGE_DIR') and (Path(os.environ['GET_FILES_CONTEXT_FORGE_DIR'])/'pdf_ocr.py').is_file(),
                         'set GET_FILES_CONTEXT_FORGE_DIR to a Context Forge checkout to compare the vendored adapter')
    def test_snapshot_matches_shared_canonical_adapter(self):
        canonical=Path(os.environ['GET_FILES_CONTEXT_FORGE_DIR'])
        vendor=Path(__file__).resolve().parents[1]/'corpus/vendor'
        for name in ['pdf_ocr.py','windows_pdf_ocr.ps1']:
            self.assertEqual((canonical/name).read_text(encoding='utf-8'),(vendor/name).read_text(encoding='utf-8'))

    def test_backend_failure_preserves_native_text(self):
        def fail(*args):raise RuntimeError('private error text')
        result=apply_pdf_ocr('unused',Extraction([Section('page:1','native'),Section('page:2','')]),backend=fail)
        self.assertEqual([s.text for s in result.sections],['native',''])
        self.assertNotIn('private error text',str(result))
        self.assertEqual(result.metadata['ocr']['unresolved_pages'],[2])

    def test_older_parent_does_not_receive_ocr_under_old_readiness_rules(self):
        from pypdf import PdfWriter
        from corpus.extraction_limits import bounded_extract
        with working_directory(TEST_PARENT) as root:
            source=root/'old-parent.pdf';writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.write(source)
            result=bounded_extract(source)  # Old caller has no explicit allow_ocr flag.
            self.assertNotIn('ocr',result.metadata)
            self.assertEqual(result.sections[0].text,'')

    def test_document_from_before_ocr_existed_can_continue(self):
        with working_directory(TEST_PARENT) as root:
            library=Library(root/'data')
            job={'status':'needs_ocr','document_id':'a'*64,'collection':'legal'}
            def saved(empty_pages):
                return {'format':'.pdf','manifest':{'metadata':{'pages':4},'empty_pages':empty_pages}}
            with patch.object(library,'job',return_value=job),patch.object(library,'enqueue',return_value={'id':'new'}) as enqueue:
                with patch.object(library,'document',return_value=saved(['page:1','page:2'])):
                    self.assertEqual(library.continue_ocr('b'*32),{'id':'new'})
                self.assertEqual(enqueue.call_args.args[1]['ocr_document_id'],'a'*64)
                with patch.object(library,'document',return_value=saved([])),self.assertRaises(ValueError):
                    library.continue_ocr('c'*32)

    # Timing-dependent: the 66-page fixture needs one full 64-page OCR call inside the
    # adapter's 75 s budget and 120 s worker deadline. On a slow or loaded machine, or
    # when the Windows OCR engine is unavailable or misbehaves, a page is left
    # unrecognized and the job reports needs_ocr instead of needs_review. Opt in explicitly.
    @unittest.skipUnless(os.name=='nt' and os.environ.get('GET_FILES_SLOW_OCR_TESTS')=='1',
                         'set GET_FILES_SLOW_OCR_TESTS=1 on Windows with an OCR language pack to run this timing-dependent 66-page test')
    def test_saved_nine_page_job_continues_without_download_or_overwrite(self):
        from PIL import Image, ImageDraw, ImageFont
        with working_directory(TEST_PARENT) as root:
            source=root/'long.pdf';image=Image.new('RGB',(1200,700),'white')
            ImageDraw.Draw(image).text((80,100),'SYNTHETIC LONG PDF 18123456',fill='black',font=ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf',46))
            image.save(source,'PDF',resolution=150,save_all=True,append_images=[image]*65)  # 66 pages: the shared adapter attempts 64 per call
            library=Library(root/'data');processor=Processor(library,allowed_roots=[root])
            original_job=library.enqueue('research',{'path':str(source),'mode':'both','tags':['synthetic']})
            first=processor.process(library.claim())
            self.assertEqual(first['status'],'needs_ocr',first)
            saved=library.known_document(first['document_id'])
            old_key=saved['normalized_key'];old_hash=saved['normalized_sha256']
            self.assertEqual(saved['metadata']['ocr']['pending_pages'],[65,66])
            resumed=library.continue_ocr(original_job['id'])
            self.assertEqual(library.continue_ocr(original_job['id'])['id'],resumed['id'])
            with patch.object(processor.downloader,'download',side_effect=AssertionError('Must not download')):
                second=processor.process(library.claim())
            self.assertEqual(second['status'],'needs_review',second)
            latest=library.known_document(first['document_id'])
            self.assertEqual(latest['metadata']['ocr']['current_batch_pages'],[65,66])
            self.assertEqual(latest['metadata']['ocr']['recognized_pages'],list(range(1,67)))
            self.assertEqual(latest['metadata']['ocr']['status'],'complete')
            self.assertNotEqual(latest['normalized_key'],old_key)
            self.assertEqual(hashlib.sha256(library.artifact_path(old_key).read_bytes()).hexdigest(),old_hash)
            self.assertEqual(library.continue_ocr(original_job['id'])['id'],resumed['id'])
            with self.assertRaises(ValueError):library.continue_ocr(second['id'])
            with self.assertRaises(ValueError):library.enqueue('research',{'ocr_document_id':first['document_id'],'mode':'corpus'})
            self.assertEqual(library.search('research','18123456'),[])
            self.assertIn('synthetic',library.document(first['document_id'],'research')['tags'])

    @unittest.skipUnless(os.name=='nt','Windows OCR integration')
    def test_scanned_and_mixed_pages_normalize_in_bounded_worker(self):
        from PIL import Image, ImageDraw, ImageFont
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        with working_directory(TEST_PARENT) as root:
            scan=root/'scan.pdf'
            image=Image.new('RGB',(1400,900),'white');draw=ImageDraw.Draw(image)
            draw.text((80,120),'SCANNED PATENT 18123456',fill='black',font=ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf',52))
            image.save(scan,'PDF',resolution=150)
            writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            stream=DecodedStreamObject();stream.set_data(b'BT /F1 20 Tf 40 700 Td (NATIVE EXACT TEXT) Tj ET')
            page[NameObject('/Contents')]=writer._add_object(stream)
            writer.append(str(scan));mixed=root/'mixed.pdf';writer.write(mixed)
            original_hash=hashlib.sha256(mixed.read_bytes()).hexdigest()
            library=Library(root/'data');processor=Processor(library,allowed_roots=[root])
            manifest=processor.normalize_file(mixed,'.pdf',{'title':'Synthetic mixed PDF'}, {})
            self.assertEqual(manifest['status'],'needs_review')
            self.assertEqual(manifest['normalizer'],PDF_NORMALIZER_VERSION)
            self.assertEqual(manifest['metadata']['ocr']['recognized_pages'],[2])
            self.assertEqual(manifest['empty_pages'],[])
            self.assertEqual(manifest['document_id'],original_hash)
            text=library.artifact_path(manifest['normalized_key']).read_text(encoding='utf-8')
            self.assertIn('NATIVE EXACT TEXT',text);self.assertIn('18123456',text)
            self.assertEqual(hashlib.sha256(mixed.read_bytes()).hexdigest(),original_hash)
            # Same version reuses normalized artifacts, no OCR rerun.
            with patch('corpus.extraction_limits.bounded_extract',side_effect=AssertionError('should reuse')):
                self.assertEqual(processor.normalize_file(mixed,'.pdf',{}, {})['document_id'],original_hash)

if __name__=='__main__':unittest.main()
