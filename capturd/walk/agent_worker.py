"""Isolated agent recorder: parent owns its process group and cancellation."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from capturd.walk.recorder import DemoRecorder


def main() -> None:
    args = json.load(sys.stdin)
    args['output_dir'] = Path(args['output_dir'])
    voice_enabled = args.pop('voice_enabled', False)
    recorder = DemoRecorder(**args)
    if voice_enabled:
        from capturd.walk.voice import VoiceConfig, VoiceLoop
        recorder.voice_loop = VoiceLoop(config=VoiceConfig(workflow_mode=recorder.workflow_mode))
    asyncio.run(recorder.agent_record())


if __name__ == '__main__':
    main()
