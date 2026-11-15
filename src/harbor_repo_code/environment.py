"""Docker phase isolation without nftables: detach task network endpoints.

Only public/no-network, Linux single-container tasks are supported. No allowlist
is emulated. The official task image, filesystem and verifier remain unchanged.
"""
import asyncio
import json
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode


class DockerPhaseEnvironment(DockerEnvironment):
    @classmethod
    def _requires_egress_control(cls, **kwargs):
        # This provider enforces policies with the Docker daemon, not a sidecar.
        return False

    @property
    def capabilities(self):
        return super().capabilities.model_copy(update={
            'stream':False,'disable_internet':True,'dynamic_network_policy':True,
            'network_allowlist':False,'docker_compose':False,'windows':False})

    async def _docker(self,*args):
        process=await asyncio.create_subprocess_exec('docker',*args,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            stdout,stderr=await asyncio.wait_for(process.communicate(),timeout=60)
        except BaseException:
            if process.returncode is None:process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError('Docker phase policy failed: '+stderr.decode(errors='replace')[-1500:])
        return stdout.decode().strip()

    async def start(self,force_build):
        if self._uses_compose or self._is_windows_container or self.extra_docker_compose_paths:
            raise ValueError('Phase-detach backend supports Linux single-container tasks only')
        self._saved_networks={}
        self._policy_events=[]
        await super().start(force_build)
        result=await self._run_docker_compose_command(['ps','-q','main'])
        self._owned_container=(result.stdout or '').strip()
        if not self._owned_container or '\n' in self._owned_container:
            raise RuntimeError('Cannot identify owned task container')
        self._image_identity=json.loads(await self._docker('inspect',self._owned_container))[0]['Image']
        await self._apply_network_policy(self.network_policy)

    async def _apply_network_policy(self,network_policy):
        if network_policy.network_mode not in {NetworkMode.NO_NETWORK,NetworkMode.PUBLIC}:
            raise ValueError('Phase-detach backend does not implement network allowlists')
        cid=self._owned_container
        record=json.loads(await self._docker('inspect',cid))[0]
        attached=record['NetworkSettings']['Networks']
        if network_policy.network_mode==NetworkMode.NO_NETWORK:
            for name,settings in attached.items():
                self._saved_networks.setdefault(name,settings)
                await self._docker('network','disconnect',name,cid)
            after=json.loads(await self._docker('inspect',cid))[0]['NetworkSettings']['Networks']
            if after:raise RuntimeError('Task network endpoints remain attached')
            probe=await self.exec("python3 -c \"import json,os,socket; s=socket.socket();s.settimeout(1);denied=False\ntry:s.connect(('1.1.1.1',443))\nexcept OSError:denied=True\nfinally:s.close()\nprint(json.dumps({'interfaces':os.listdir('/sys/class/net'),'denied':denied}))\"",timeout_sec=10)
            observed=json.loads(probe.stdout or '{}')
            if probe.return_code or observed.get('interfaces')!=['lo'] or observed.get('denied') is not True:
                raise RuntimeError('Offline phase network probe failed')
        else:
            for name,settings in self._saved_networks.items():
                if name not in attached:
                    args=['network','connect']
                    for alias in settings.get('Aliases') or []:args+=['--alias',alias]
                    await self._docker(*args,name,cid)
            after=json.loads(await self._docker('inspect',cid))[0]['NetworkSettings']['Networks']
            observed={'interfaces_attached':sorted(after)}
        self._policy_events.append({'mode':network_policy.network_mode.value,'container':cid,
            'image':self._image_identity,'observation':observed})
        path=self.trial_paths.trial_dir/'network-policy.json'
        path.write_text(json.dumps(self._policy_events,indent=2),encoding='utf-8')
