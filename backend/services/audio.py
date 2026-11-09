"""Local decode validation and a lossless mono recognition copy. Original audio is untouched."""
import re
import subprocess
import imageio_ffmpeg


def command(args,timeout):
    try:
        return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-hide_banner',*args],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:raise ValueError('音频处理超时，原音频保留，可检查文件后重试')


def inspect(path):
    result=command(['-i',str(path)],30)
    output=result.stderr.decode('utf-8',errors='replace')
    tracks=re.findall(r'Stream #0:\d+[^\n]*Audio:[^\n]*',output)
    if not tracks:raise ValueError('文件没有可识别的音轨或容器损坏，尚未上传云端')
    match=re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)',output)
    duration=sum(float(v)*scale for v,scale in zip(match.groups(),(3600,60,1))) if match else None
    if duration and duration>10801:raise ValueError('音频超过本版三小时范围，请先分段，原文件保留')
    return {'tracks':len(tracks),'duration':duration,'description':tracks,
            'processing':'所有音轨混合为单声道无损识别副本；原音频保留'}


def prepare(path,dest):
    info=inspect(path)
    count=info['tracks']
    args=['-y','-i',str(path)]
    if count>1:
        graph=''.join(f'[0:a:{i}]aformat=sample_rates=48000:channel_layouts=mono[a{i}];' for i in range(count))
        graph+=''.join(f'[a{i}]' for i in range(count))+f'amix=inputs={count}:duration=longest:normalize=1[out]'
        args+=['-filter_complex',graph,'-map','[out]']
    else:args+=['-map','0:a:0']
    args+=['-vn','-ac','1','-ar','48000','-c:a','flac',str(dest)]
    result=command(args,1800)
    if result.returncode or not dest.exists() or dest.stat().st_size<20:
        dest.unlink(missing_ok=True)
        raise ValueError('音频无法完整解码，尚未提交识别；原文件保留，请检查录音完整性')
    return info
