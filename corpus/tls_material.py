"""Generate new spoke identities into an empty, access-restricted directory.

Never replaces credentials. The CLI reports paths and fingerprints, not keys.
"""
import datetime as dt
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def restrict_to_current_user(path):
    """Limit a private-key file to the current user (owner-only mode on POSIX, icacls on Windows)."""
    if sys.platform!='win32':
        os.chmod(path,0o600);return
    user=os.environ.get('USERNAME','')
    if os.environ.get('USERDOMAIN'):user=os.environ['USERDOMAIN']+'\\'+user
    icacls=Path(os.environ.get('SYSTEMROOT',r'C:\Windows'))/'System32'/'icacls.exe'
    result=subprocess.run([str(icacls),str(path),'/inheritance:r','/grant:r',user+':F'],capture_output=True,timeout=30)
    if not user or result.returncode:raise RuntimeError('Could not restrict a private key file to the current user')


def create_material(directory,collections=('research','legal')):
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    if any(directory.iterdir()):raise ValueError('Certificate directory must be empty; existing credentials are never replaced')
    now=dt.datetime.now(dt.timezone.utc)
    def name(label):return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,label)])
    ca_key=ec.generate_private_key(ec.SECP256R1());ca_name=name('Get Files local spoke CA')
    ca=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-dt.timedelta(minutes=5)).not_valid_after(now+dt.timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256()))
    def leaf(label,usage):
        key=ec.generate_private_key(ec.SECP256R1())
        builder=(x509.CertificateBuilder().subject_name(name(label)).issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-dt.timedelta(minutes=5)).not_valid_after(now+dt.timedelta(days=90)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False),critical=True).add_extension(x509.ExtendedKeyUsage([usage]),critical=False).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False))
        if usage==ExtendedKeyUsageOID.SERVER_AUTH:
            builder=builder.add_extension(x509.SubjectAlternativeName([x509.DNSName('host.docker.internal'),x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
        return key,builder.sign(ca_key,hashes.SHA256())
    server_key,server=leaf('Get Files restricted spoke',ExtendedKeyUsageOID.SERVER_AUTH)
    client_key,client=leaf('Get Files n8n client',ExtendedKeyUsageOID.CLIENT_AUTH)
    for label,key,cert in [('ca',ca_key,ca),('server',server_key,server),('n8n-client',client_key,client)]:
        for suffix,value in [('-key.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),('.pem',cert.public_bytes(serialization.Encoding.PEM))]:
            with (directory/(label+suffix)).open('xb') as stream:stream.write(value)
            if suffix=='-key.pem':restrict_to_current_user(directory/(label+suffix))
    fingerprint=client.fingerprint(hashes.SHA256()).hex()
    config={'host':'127.0.0.1','port':8767,'ca_file':str(directory/'ca.pem'),'cert_file':str(directory/'server.pem'),'key_file':str(directory/'server-key.pem'),'clients':{fingerprint:{'collections':list(collections),'actions':['ingest','get_files','search','tags','job','read_source','original']}}}
    with (directory/'spoke.json').open('x',encoding='utf-8') as stream:json.dump(config,stream,indent=2)
    return config


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);args=parser.parse_args()
    config=create_material(args.directory)
    print(json.dumps({'config':str(args.directory.resolve()/'spoke.json'),'client_fingerprints':list(config['clients']),'client_lifetime_days':90,'grant_lifetime_seconds':60}))
