# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/4A. S2A dataset preparation.ipynb.

# %% auto 0
__all__ = ['flac_to_s2a_name']

# %% ../nbs/4A. S2A dataset preparation.ipynb 2
import sys
import os
import itertools
from pathlib import Path

import numpy as np
import torch
import torchaudio
import torch.nn.functional as F
from torch.profiler import profile, record_function, ProfilerActivity

from fastprogress import progress_bar
from fastcore.script import *

import whisper
from . import vad, wh_transcribe, vq_stoks, extract_acoustic
import webdataset as wds

# %% ../nbs/4A. S2A dataset preparation.ipynb 4
def flac_to_s2a_name(input):
    if '-flac-' in input:
        return input.rsplit("/", 1)[1].replace('flac', 's2a') + ".gz"
    else:
        return input.rsplit("/", 1)[1].replace('raw', 's2a') + ".gz"

# %% ../nbs/4A. S2A dataset preparation.ipynb 6
def resampler(newsr = 24000, key = 'samples_24k'):
    _last_sr = None
    tform = None
    
    def _resample(samples):
        for s in samples:
            sr = s['sample_rate']
            if sr != newsr:
                if sr != _last_sr: tform = torchaudio.transforms.Resample(sr, newsr)
                s[key] = tform(s['samples'])
            else:
                s[key] = s['samples']
            yield s
    
    return _resample

# %% ../nbs/4A. S2A dataset preparation.ipynb 9
@call_parse
def prepare_s2a(
    input:str,  # FLAC webdataset file path (or - to read the names from stdin)
    proc_dataset_path:Path, # processed VAD files path
    output:str=None, # output file name
    vq_model:str="collabora/spear-tts-pytorch:whisper-vq-stoks.model", # the model path (use repo_id:filename to download it from hugginface)
    n_samples:int=None, # process a limited amount of samples
    batch_size:int=1, # process several segments at once
    fix_dots:bool=False, # fix dots in file names
):
    if ":" in vq_model:
        repo, fname = vq_model.split(":", 1)
        vq_model = vq_stoks.RQBottleneckTransformer.load_model(repo, fname).cuda()
    else:
        vq_model = vq_stoks.RQBottleneckTransformer.load_model(local_filename=vq_model).cuda()
    amodel = extract_acoustic.load_model()
    amodel.set_target_bandwidth(3)

    if input == "-":
        input = [f.strip() for f in sys.stdin.readlines()]
        assert output, "please provide the output shard name"
    else:
        if output is None: output = flac_to_s2a_name(input)
        input = [input]
        
    total = n_samples//batch_size if n_samples else 'noinfer'

    ds = wds.WebDataset(input, shardshuffle=True, rename_files=vad.fix_dots_in_names if fix_dots else None).compose(
        wds.decode(wds.torch_audio),
        wds.select(lambda x: 'wav' in x or 'flac' in x),
        vq_stoks.merge_in(vq_stoks.derived_dataset(proc_dataset_path, 'vad')),
        wds.map_dict(**{"vad.npy":wh_transcribe.chunk_merger}),
        lambda x: wh_transcribe.split_to_chunks(x),
        resampler(),
        resampler(16000, 'samples_16k'),
        wds.to_tuple('__key__', 'rpad_s', 'samples_16k', 'samples_24k'),
        wds.batched(64),
    )

    dl = wds.WebLoader(ds, num_workers=4, batch_size=None).unbatched().shuffle(2000).batched(batch_size)

    speakers = set()
    tmp = output+".tmp"
    with wds.TarWriter(tmp) as sink:
        for keys, rpad_ss, samples, samples24k in progress_bar(dl, total=total):
            with record_function('to_cuda'):
                samples, samples24k = samples.cuda(), samples24k.unsqueeze(1).cuda()
            with record_function('encodec'):
                atoks = amodel.encode(samples24k)[0][0]
            with record_function('vq_stoks'):
                stoks = vq_model.encode_audio(samples)
            with record_function('from_cuda'):
                atoks, stoks = atoks.cpu().numpy().astype(np.int16), stoks.cpu().numpy().astype(np.int16)
            for key, rpad_s, _atoks, _stoks in zip(keys, rpad_ss, atoks, stoks):
                speakers.add(key.split('/')[1])
                sink.write({
                    "__key__": key,
                    "atoks.npy": _atoks[:,:int(-rpad_s * 75)],
                    "stoks.npy": _stoks[:int(-rpad_s * 25)],
                })
    with open(output+".speakers.txt", "w") as f: f.write("\n".join(speakers))
    if not n_samples:
        os.rename(tmp, output)