"""Collect local arm diagnostics into a ZIP. GET requests only; never commands motion."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from urllib.request import urlopen
import zipfile


def run_readonly(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=10)
        return result.stdout, result.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return '', str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    out = args.output or Path.home() / 'Downloads' / f'arm_debug_{stamp}.zip'
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, 'x', zipfile.ZIP_DEFLATED) as z:
        z.writestr('collection.txt', f'Collected now: {stamp} UTC\nProject: {root}\n')
        for name, command in [('version.txt', ['git','log','-5','--oneline']),
                              ('git_status.txt', ['git','status','--short','--branch'])]:
            stdout, stderr = run_readonly(command, cwd=root)
            z.writestr(name, stdout + stderr)
        stdout, stderr = run_readonly(['ps','-eo','pid,args'])
        running = []
        for line in stdout.splitlines():
            if 'arm.click' in line or 'grab_text.py' in line:
                running.append(line)
                try:
                    cwd = (Path('/proc') / line.split()[0] / 'cwd').resolve(strict=True)
                    running.append('Working folder: ' + str(cwd))
                except OSError:
                    pass
        z.writestr('running_programs.txt', '\n'.join(running) + '\n' + stderr)
        files = list((root/'controller/calib').glob('*.json'))
        files += list((root/'pick_views').glob('hand_latest_*.jpg'))
        files += [root/'pick_views/latest.jpg', root/'pick_views/standing_input.jpg',
                  root/'pick_views/selected_mask.png', root/'pick_views/wrist_alignment.json', root/'grab_text.py',
                  root/'small_object_grasp.py', root/'pickup_test.log']
        files += [root/'controller/arm'/name for name in
                  ('reach.py','click.py','arm.py','kinematics.py','vision_contract.py')]
        reports = sorted((root/'config/local/grasp_runs').glob('*.json'),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files + reports[:3]:
            if p.is_file():
                z.write(p, str(p.relative_to(root)))
        for name in ('status','pose','model'):
            try:
                with urlopen('http://127.0.0.1:8890/' + name, timeout=3) as response:
                    z.writestr('current_' + name + '.json', response.read())
            except Exception as exc:
                z.writestr('current_' + name + '_error.txt', str(exc))
    print(f'Saved: {out}\nTransfer this ZIP to your computer and attach it in the chat.')


if __name__ == '__main__':
    main()
