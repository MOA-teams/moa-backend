"""GitHub Secrets의 S3 설정을 SSH stdin으로 전달하고 전용 systemd drop-in에 저장한다."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

CONFIG_KEYS = ("AWS_REGION", "S3_BUCKET_NAME", "S3_PRESIGNED_URL_EXPIRE_SECONDS")
DROPIN_DIRECTORY = Path("/etc/systemd/system/moa-backend.service.d")
DROPIN_FILE = DROPIN_DIRECTORY / "zz-moa-s3.conf"


def validate_config(raw: dict) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) - set(CONFIG_KEYS):
        raise ValueError("설정 형식 오류")
    region = raw.get("AWS_REGION")
    bucket = raw.get("S3_BUCKET_NAME")
    ttl = raw.get("S3_PRESIGNED_URL_EXPIRE_SECONDS")
    if ttl is None or ttl == "":
        ttl = "300"
    if not all(isinstance(value, str) for value in (region, bucket, ttl)):
        raise ValueError("설정 형식 오류")
    region, bucket, ttl = region.strip(), bucket.strip(), ttl.strip()
    # 허용 문자를 제한해 shell/systemd 지시문·specifier가 설정에 삽입되지 않게 한다.
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", region):
        raise ValueError("AWS_REGION 오류")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or any(value in bucket for value in ("..", ".-", "-.")):
        raise ValueError("S3_BUCKET_NAME 오류")
    if not re.fullmatch(r"[0-9]{1,4}", ttl) or not 1 <= int(ttl) <= 3600:
        raise ValueError("S3_PRESIGNED_URL_EXPIRE_SECONDS 오류")
    return {"AWS_REGION": region, "S3_BUCKET_NAME": bucket, "S3_PRESIGNED_URL_EXPIRE_SECONDS": str(int(ttl))}


def install_config(config: dict[str, str]) -> None:
    content = "[Service]\n" + "".join(f'Environment="{key}={value}"\n' for key, value in config.items())
    # 임시 파일은 0600이며 sudo 명령 인자·로그에 설정 값이 포함되지 않는다.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as temporary:
        temporary.write(content)
        temporary.flush()
        subprocess.run(["sudo", "install", "-d", "-m", "0755", str(DROPIN_DIRECTORY)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "install", "-m", "0600", temporary.name, str(DROPIN_FILE)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    try:
        if sys.argv[1:] == ["--export"]:
            config = validate_config({key: os.environ.get(key, "") for key in CONFIG_KEYS})
            # stdout은 반드시 SSH stdin에 연결한다. workflow 로그·명령 인자에 값을 넣지 않는다.
            json.dump(config, sys.stdout)
        elif not sys.argv[1:]:
            payload = sys.stdin.read(16385)
            if len(payload) > 16384:
                raise ValueError("설정 크기 오류")
            config = validate_config(json.loads(payload))
            install_config(config)
            print("S3 서비스 설정 저장 완료")
        else:
            raise ValueError("지원하지 않는 실행 모드")
    except (ValueError, OSError, subprocess.CalledProcessError):
        print("S3 설정 반영 실패: AWS_REGION·S3_BUCKET_NAME·만료 시간과 서버 설정 쓰기 권한을 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
