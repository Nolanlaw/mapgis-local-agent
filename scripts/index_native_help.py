"""Build local indexes from the recipient's own MapGIS installation.

HLP parsing runs the separate, unmodified GPL-2.0 winhlp tool sources.
CHM extraction uses Windows hh.exe; extracted HTML is read as text, never run.
No proprietary help or generated index is part of the distributed package.
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mapgis_agent.environment import MODULES

# This installed Chinese report HLP declares only ANSI. Apply its known code
# page by exact source digest, never by filename or a language guess.
HLP_ENCODING_PROFILES = {
    '81d9b00979a8e29f2690dc075f204a9533317d015dd7c8f387ada1127c9ee951': 'gb18030',
}


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def index_module(module: str, source: Path, folder: Path, *, hlp_encoding: str | None = None) -> dict:
    baseline = hashlib.sha256(source.read_bytes()).hexdigest()
    encoding_source = 'explicit' if hlp_encoding is not None else 'source-metadata'
    if hlp_encoding is None and baseline in HLP_ENCODING_PROFILES:
        hlp_encoding = HLP_ENCODING_PROFILES[baseline]
        encoding_source = 'exact-source-profile'
    errors = []
    if source.suffix.casefold() == '.hlp':
        sys.path.insert(0, str(ROOT / 'third_party/winhlp/src'))
        from winhlp.lib.hlp import HelpFile
        if hlp_encoding is not None:
            # Some Chinese HLP files declare only ANSI. Override decoding before
            # phrase/topic parsing, explicitly; never rewrite the installed file
            # or guess another language from a garbled Unicode result.
            codecs.lookup(hlp_encoding)

            class EncodedHelpFile(HelpFile):
                def _guess_undeclared_code_page(self):
                    if self.system is None:
                        raise ValueError('HLP_SYSTEM_MISSING_FOR_ENCODING_OVERRIDE')
                    self.system.encoding = hlp_encoding

            document = EncodedHelpFile(filepath=str(source))
        else:
            document = HelpFile(filepath=str(source))
        topics = [{'id': str(i), 'title': t.title, 'text': t.get_plain_text(),
                   'contexts': t.context_names} for i, t in enumerate(document.topic.get_all_topics())]
        errors = document.parse_errors
        parser = 'winhlp cb2661ef85693799641cf7ab258e7a4074732568; parser API subset'
    else:
        destination = folder / ('chm-' + module + '-' + baseline[:12])
        marker = destination / 'extracted.json'
        if not marker.is_file():
            destination.mkdir(parents=True, exist_ok=True)
            hh = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'hh.exe'
            # hh.exe can silently produce no files when the output argument has
            # spaces. A relative output in the intended cwd also permits retry
            # after an incomplete extraction without deleting generated files.
            proc = subprocess.Popen([str(hh), '-decompile', '.', str(source.resolve())], cwd=destination)
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
                raise RuntimeError('CHM_EXTRACTION_TIMEOUT')
            if code:
                raise RuntimeError('CHM_EXTRACTION_FAILED: ' + str(code))
            # hh.exe can return before its worker finishes writing HTML.
            previous = None
            for _ in range(20):
                current = [(str(p), p.stat().st_size) for p in destination.rglob('*') if p.is_file()]
                if current and current == previous:
                    break
                previous = current
                time.sleep(.25)
            if not list(destination.rglob('*.htm')) and not list(destination.rglob('*.html')):
                raise RuntimeError('CHM_CONTAINS_NO_HTML')
            marker.write_text(json.dumps({'source_sha256': baseline}), encoding='utf8')
        topics = []
        for file in sorted(destination.rglob('*')):
            if file.suffix.casefold() not in ('.htm', '.html') or file.is_symlink():
                continue
            data = file.read_bytes()
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('gb18030')
            reader = TextOnly()
            reader.feed(text)
            plain = '\n'.join(line.strip() for line in ''.join(reader.parts).splitlines() if line.strip())
            topics.append({'id': file.relative_to(destination).as_posix(),
                           'title': plain.split('\n')[0] if plain else file.stem, 'text': plain})
        parser = 'Windows hh.exe -decompile + Python HTMLParser; no HTML execution'
    if hashlib.sha256(source.read_bytes()).hexdigest() != baseline:
        raise RuntimeError('HELP_CHANGED_DURING_INDEXING')
    result = {'module': module, 'source': str(source.resolve()), 'source_sha256': baseline,
              'parser': parser, 'parse_errors': errors, 'topics': topics}
    if source.suffix.casefold() == '.hlp':
        result['encoding'] = document.system.encoding if document.system else None
        result['encoding_override'] = hlp_encoding
        result['encoding_source'] = encoding_source
    target = folder / (module + '.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding='utf8')
    os.replace(temporary, target)
    return {'module': module, 'status': 'PARTIAL_INDEX' if errors else 'INDEXED',
            'topics': len(topics), 'parse_errors': errors, 'source_sha256': baseline}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', choices=list(MODULES))
    parser.add_argument('--hlp-encoding', help='Explicit HLP code page, e.g. gb18030 for an ANSI-tagged Chinese help file')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf8'))
    program = Path(config['mapgis_installation']) / 'program'
    folder = ROOT / 'work/native-help'
    folder.mkdir(parents=True, exist_ok=True)
    result = []
    for module, (_, name) in MODULES.items():
        if args.module and module != args.module:
            continue
        source = program / name
        if not source.is_file():
            result.append({'module': module, 'status': 'HELP_NOT_INSTALLED'})
            continue
        try:
            result.append(index_module(module, source, folder, hlp_encoding=args.hlp_encoding))
        except Exception as exc:
            result.append({'module': module, 'status': 'ERROR', 'error': str(exc)})
    print(json.dumps(result, ensure_ascii=False, default=str))
    if any(item['status'] == 'ERROR' for item in result):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
