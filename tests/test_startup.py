import os
import http.client
import json
import socket
import ssl
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests import TEST_PARENT
from corpus.processing import working_directory
from corpus.tls_material import create_material


class StartupTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32','The real PowerShell launcher is only available on Windows')
    def test_real_launcher_uses_fixture_storage_and_shuts_down_cleanly(self):
        with working_directory(TEST_PARENT) as root:
            data=root/'data';data.mkdir();token='fixture-startup-'+'x'*48
            (data/'service-token.txt').write_text(token)
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));tls_port=probe.getsockname()[1]
            self.assertNotEqual(port,8766)
            config=create_material(root/'identities');config.update(host='127.0.0.1',port=tls_port)
            config_file=root/'identities'/'spoke.json';config_file.write_text(json.dumps(config))
            result=subprocess.run(['powershell.exe','-NoProfile','-File','start.ps1','-Background','-Port',str(port),'-DataPath',str(data),'-SpokeConfig',str(config_file)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45,env={**os.environ,'GET_FILES_PYTHON':sys.executable})
            state_file=data/'service-state.json'
            def request(route,method='GET',body=None):
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                try:
                    connection.request(method,route,json.dumps(body) if body is not None else None,{'Content-Type':'application/json','Authorization':'Bearer '+token})
                    response=connection.getresponse();return response.status,json.loads(response.read())
                finally:connection.close()
            try:
                self.assertEqual(result.returncode,0,'Fixture startup failed; inspect fixture logs')
                self.assertTrue(state_file.exists())
                health=request('/health')[1];self.assertTrue(health['ok']);self.assertEqual(health['version'],'1.2-scoped')
                state=json.loads(state_file.read_text(encoding='utf-8-sig'))
                self.assertIsInstance(health['process_id'],int)
                self.assertEqual(state['pid'],health['process_id'])
                self.assertTrue(health['accepting_jobs'])
                tls=ssl.create_default_context(cafile=config['ca_file']);tls.load_cert_chain(root/'identities'/'n8n-client.pem',root/'identities'/'n8n-client-key.pem')
                connection=http.client.HTTPSConnection('127.0.0.1',tls_port,context=tls,timeout=3)
                try:
                    connection.request('GET','/health');response=connection.getresponse();self.assertEqual(response.status,200);self.assertTrue(json.loads(response.read())['ok'])
                finally:connection.close()
                self.assertEqual(request('/v1/admin/shutdown','POST',{})[0],202)
                for _ in range(50):
                    try:request('/health');time.sleep(.1)
                    except OSError:break
                else:self.fail('Fixture service did not stop')
            finally:
                # Only stop the fixture process if graceful shutdown failed.
                if state_file.exists():
                    state=json.loads(state_file.read_text(encoding='utf-8-sig'))
                    code="$p=Get-CimInstance Win32_Process -Filter 'ProcessId="+str(state['pid'])+"'; if ($p -and $p.CommandLine.Contains('"+str(data)+"')) { Stop-Process -Id "+str(state['pid'])+" -ErrorAction SilentlyContinue }"
                    subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,timeout=15)


if __name__=='__main__':unittest.main()


class InstallScriptContractTests(unittest.TestCase):
    """Static checks only: install.ps1 is never executed by the tests."""
    root=Path(__file__).resolve().parents[1]

    def test_scripts_parse(self):
        if sys.platform!='win32':self.skipTest('PowerShell parser check is Windows-only')
        for name in ['install.ps1','start.ps1','scripts/runtime.ps1']:
            command="$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('"+str(self.root/name)+"',[ref]$null,[ref]$e);if($e){exit 1}"
            self.assertEqual(subprocess.run(['powershell.exe','-NoProfile','-Command',command],timeout=30).returncode,0,name)

    def test_data_directory_acl_is_applied_only_to_new_or_empty_directories(self):
        text=(self.root/'install.ps1').read_text(encoding='utf-8')
        lines=text.splitlines()
        acl_lines=[i for i,line in enumerate(lines) if 'icacls.exe $DataPath' in line]
        self.assertEqual(len(acl_lines),1)
        self.assertTrue(any(l.strip().startswith('if ($dataIsEmpty)') for l in lines[:acl_lines[0]]))
        self.assertIn('Write-Warning',text[text.index('if ($dataIsEmpty)'):])
        self.assertLess(text.index('$dataIsEmpty='),text.index('New-Item -ItemType Directory -Force -Path $DataPath'))
