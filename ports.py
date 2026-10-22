"""Check local listeners and Windows exclusions before binding."""
import re
import subprocess
import sys

def check_port(port):
    if port in (8765, 8766):
        raise OSError('This port is reserved for AnkiConnect or the OCR bridge.')
    if sys.platform != 'win32':
        raise OSError('Automatic port reservation checks currently support Windows only.')
    def output(args):
        return subprocess.check_output(args, text=True, errors='replace', timeout=10,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        listeners = output(['netstat', '-ano'])
        for line in listeners.splitlines():
            cols = line.split()
            # Only listeners and bound UDP sockets block us; TIME_WAIT and client
            # connections left from a previous run do not. Listeners have no
            # remote address, which avoids parsing netstat's localized state.
            listening = len(cols) > 3 and (cols[0] == 'UDP' or cols[2] in ('0.0.0.0:0', '[::]:0'))
            if listening and cols[1].endswith(f':{port}'):
                raise OSError(f'Port {port} is already in use (PID {cols[-1]}).')
        for family in ('ipv4', 'ipv6'):
            for protocol in ('tcp', 'udp'):
                ranges = output(['netsh', 'interface', family, 'show', 'excludedportrange', f'protocol={protocol}'])
                for first, last in re.findall(r'^\s*(\d+)\s+(\d+)\s*\*?\s*$', ranges, re.M):
                    if int(first) <= port <= int(last):
                        raise OSError(f'Port {port} is reserved by Windows.')
    except subprocess.SubprocessError as error:
        raise OSError('Could not inspect port reservations. Server was not started.') from error
