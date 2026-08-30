# Third-party notice

This image includes an LF-normalized copy of `mcp_fastqc/app/fastqc_server.py` from
[florensiawidjaja/BioinfoMCP](https://github.com/florensiawidjaja/BioinfoMCP)
at commit `7ada7918b9e515604d3c0ae264d3a9af10bf6e54`.

After verifying the upstream file checksum, the image applies one local fix:
generated `*_fastqc.html` and `*_fastqc.zip` paths are returned in the MCP
tool's `output_files` field. The upstream source currently leaves that result
collection block commented out.

The FastQC runtime comes from the digest-pinned BioContainers image
`quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0`. FastQC is distributed under
the GNU General Public License; its `LICENSE`, `LICENSE.txt`,
`LICENSE_JHDF5.txt`, source files, and runtime remain available inside the
image under `/opt/fastqc/opt/fastqc-0.12.1`.

BioinfoMCP is distributed under the MIT License:

Copyright (c) 2025 florensiawidjaja

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
