# Campus Intranet: Mini DNS Server with Local Website Hosting

[![Cisco Packet Tracer](https://img.shields.io/badge/Cisco_Packet_Tracer-v8.2%2B-blue?logo=cisco&logoColor=white)](https://www.netacad.com/courses/packet-tracer)
[![Course](https://img.shields.io/badge/Course-Computer_Networks_(5CN)-green)](https://en.wikipedia.org/wiki/Computer_network)
[![Protocols](https://img.shields.io/badge/Protocols-DNS%20|%20HTTP%20|%20TCP%20|%20UDP-orange)](#protocol-stack--ports)
[![Topology](https://img.shields.io/badge/Topology-Star%20(802.3)-purple)](#network-topology)

A complete **Computer Networks (5CN)** mini-project simulating an isolated enterprise/campus intranet in **Cisco Packet Tracer**. The project deploys a private Domain Name System (DNS) resolver alongside a dedicated Hypertext Transfer Protocol (HTTP) server, allowing departmental client workstations to access internal portals (`http://localrec`) with zero dependency on external WAN/Internet connections.

---

## 📌 Repository Contents

```bash
├── miniproj.pkt                        # Cisco Packet Tracer lab project file
├── Project_Report.docx                 # Formatted academic Word submission report
└── README.md                           # Repository documentation (this file)
```

---

## 🌐 Network Topology

The network follows a physical and logical **Star Topology** with a centralized Cisco Catalyst 2960 Layer-2 switch interconnecting dedicated infrastructure servers and departmental endpoints:

```text
                     +---------------------------+
                     |       Cisco 2960          |
                     |         Switch0           |
                     +--+--------+--------+---+--+
                        |        |        |   |
      Fa0/1 +-----------+        |        |   +-----------+ Fa0/4
            |              Fa0/2 |        | Fa0/3         |
            v                    v        v               v
  +-------------------+  +-------------+  +-------------+  +-------------+
  |    DNS-Server     |  | Web-Server  |  |     PC0     |  |     PC1     |
  |  (Authoritative)  |  |  (Intranet) |  | (CSE Dept)  |  | (ECE Dept)  |
  |   192.168.1.10    |  | 192.168.1.20|  |192.168.1.101|  |192.168.1.102|
  +-------------------+  +-------------+  +-------------+  +-------------+
```

---

## ⚙️ IP Addressing & Device Scheme

All devices operate inside the private Class C subnet **`192.168.1.0/24`** (`255.255.255.0`):

| Device | Interface | IP Address | Subnet Mask | Default Gateway | DNS Server | Role / Service |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Switch0** | Fa0/1–4 | *Unmanaged L2* | — | — | — | Layer-2 Star Distribution |
| **DNS-Server** | Fa0 | `192.168.1.10` | `255.255.255.0` | `192.168.1.1` | `192.168.1.10` | Authoritative DNS (UDP 53) |
| **Web-Server** | Fa0 | `192.168.1.20` | `255.255.255.0` | `192.168.1.1` | `192.168.1.10` | Intranet Web Host (HTTP 80) |
| **PC0 (CSE)** | Fa0 | `192.168.1.101` | `255.255.255.0` | `192.168.1.1` | `192.168.1.10` | Client Workstation (CSE) |
| **PC1 (ECE)** | Fa0 | `192.168.1.102` | `255.255.255.0` | `192.168.1.1` | `192.168.1.10` | Client Workstation (ECE) |

---

## 🏷️ Configured DNS Zone Records

Configured inside **`DNS-Server`** &rarr; **Services** &rarr; **DNS** (Service: **ON**):

| Domain Name / Record | Record Type | Target / Resolved IP | Function |
| :--- | :---: | :--- | :--- |
| `localrec` | `A Record` | `192.168.1.20` | Resolves canonical domain to Web Server IP |
| `www.localrec` | `CNAME` | `localrec` | Alias pointing `www` prefix to canonical name |

---

## 🚀 Quickstart: Running & Verifying the Lab

### 1. Prerequisites
- [Cisco Packet Tracer](https://www.netacad.com/courses/packet-tracer) (version 8.0 or higher).
- Windows / macOS / Linux.

### 2. Opening the Project
1. Clone or download this repository.
2. Double-click **`miniproj.pkt`** to launch the lab in Cisco Packet Tracer.
3. Allow 30–50 seconds for Spanning Tree Protocol (STP) to transition all switch port LEDs from **Amber** to solid **Green**.

### 3. Verification Commands

#### A. Ping Connectivity Test
Open **PC0** &rarr; **Desktop** &rarr; **Command Prompt**:
```cmd
ping 192.168.1.10
ping 192.168.1.20
```
*Expected: 4 packets transmitted, 4 received, 0% packet loss.*

#### B. DNS Name Resolution Test
In **PC0**'s Command Prompt:
```cmd
nslookup localrec
```
*Expected Output:*
```text
Server:  192.168.1.10
Address: 192.168.1.10

Name:    localrec
Address: 192.168.1.20
```

#### C. Web Browser Navigation
1. Open **PC0** or **PC1** &rarr; **Desktop** &rarr; **Web Browser**.
2. In the URL bar, type:
   ```text
   http://localrec
   ```
3. Click **Go**. The customized *"Welcome to Campus Departmental Intranet"* portal will render.

---

## 🔍 Protocol Stack & Packet Simulation

To observe packet flow under the hood:
1. Toggle to **Simulation Mode** (`Shift + S`) in the bottom-right corner.
2. Click **Edit Filters** &rarr; enable only `DNS` and `HTTP` (or add `TCP`).
3. In **PC1**'s Web Browser, enter `http://localrec` and click **Go**.
4. Step forward using the **Capture / Forward** button:

```text
[PC1 Client]
    │
    ├─ 1. DNS Query (UDP Port 53) ──────> [DNS-Server: 192.168.1.10]
    │                                            │
    │<── 2. DNS Reply ("192.168.1.20") ──────────┘
    │
    ├─ 3. TCP 3-Way Handshake (SYN) ───> [Web-Server: 192.168.1.20:80]
    │<── 4. TCP Handshake (SYN-ACK) ────────────┤
    ├─ 5. TCP Handshake (ACK) ─────────────────>│
    │                                            │
    ├─ 6. HTTP GET / (Port 80) ────────────────>│
    │<── 7. HTTP 200 OK (Transfers HTML) ───────┘
    │
[Browser Renders Departmental Portal]
```

---

## 👨‍💻 Project Information

- **Course:** Computer Networks (5CN)
- **Topic:** Application Layer Services (DNS + HTTP) Simulation
- **Platform:** Cisco Packet Tracer v8.x
- **Academic Year:** 2026–2027
