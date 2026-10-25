"""Small artifact platform contract; host installation support is separate."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LinuxPlatform:
    arch: str
    elf_machine: int

    def verify_elf(self, header):
        if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != self.elf_machine:
            raise ValueError('Custom Mihomo binary is not Linux ' + self.arch + ' ELF')


def linux_platform(arch='amd64'):
    aliases = {'amd64': ('amd64', 62), 'x86_64': ('amd64', 62),
               'arm64': ('arm64', 183), 'aarch64': ('arm64', 183)}
    if arch not in aliases:
        raise ValueError('Supported artifact architectures: amd64, arm64')
    return LinuxPlatform(*aliases[arch])
