# Multi-user Online Game (Client–Server System)

**Group:** 9
**Name:** 况旻諭  
**Student ID:** 614430005  



This project implements a **multi-user online game system** using a **client–server architecture**.
Multiple clients can connect to a central server over a real network (LAN), interact in a shared game world, and exchange messages securely.

The system satisfies all mandatory and bonus requirements of the final project, including **TLS encryption, multicast auto-discovery, and IPv4/IPv6 dual-stack support**.

---

## Features

### Core Functionalities

* Multi-client support (each client runs on a separate OS instance)
* Player registration and login
* Shared game world with rooms and items
* Player actions:

  * `LOOK` – view current room status
  * `MOVE` – move between rooms
  * `INVENTORY` – view owned items
  * `TAKE` / `DEPOSIT` – pick up or drop items
* Real-time notifications for players in the same room

### Advanced / Bonus Features

* **Secure communication using TLS (SSL)**
* **Server auto-discovery using UDP multicast**
* **IPv4 / IPv6 dual-stack TCP connections**
* Private messaging (`TELL`)
* Item transfer between players (`GIVE`)
* Game state saving and clean exit

---

## System Architecture Overview

### Client–Server Communication

* **TCP** is used for all gameplay communication
* **TLS** can be enabled to encrypt traffic
* **IPv4 and IPv6** are both supported using dual-stack sockets
* **UDP Multicast** is used for automatic server discovery in the same LAN

### High-level Architecture

```
Clients (GUI)
   |
   |  TCP / TLS (IPv4 / IPv6)
   |
Server
   |
   ├── Game Logic
   ├── Player Manager
   ├── Environment / World Manager
   └── Map Loader (map.txt)

Server → UDP Multicast → Clients (Auto-discovery)
```

---

## Game Environment

* The game world is a **2D grid map**
* Each room is identified by `(x, y)` coordinates
* Items are initially loaded from a map file (`map.txt`)
* All players start at position `(0, 0)`

### Example `map.txt`

```
0 0 Apple
1 0 Banana
1 0 Banana
1 1 Banana
1 1 Orange
1 2 Apple
2 0 Banana
2 0 Orange
```

---

## Project Structure

```
.
├── server_full.py        # Game server
├── client_gui.py         # GUI client
├── map.txt               # Game map definition
├── cert.pem              # TLS certificate
├── key.pem               # TLS private key
└── README.md
```

---

## Requirements

* Python 3.8 or later
* Linux environment recommended
* Required Python modules:

  * `socket`
  * `ssl`
  * `threading`
  * `tkinter`
  * `json`

For Ubuntu / Debian:

```bash
sudo apt install python3 python3-tk openssl
```

---

## How to Run

### 1. Start the Server

```bash
python3 server_full.py --port 7777 --map map.txt --tls --cert cert.pem --key key.pem
```

Server output example:

```
[Server] Dual-stack listening on :::7777 (IPv4+IPv6)
[Server] TLS enabled
[Server] Multicast auto-discovery: 239.255.0.1:19000
```

---

### 2. Start the Client

```bash
python3 client_gui.py
```

From the GUI:

* Choose **Discover (Multicast)** to auto-detect the server
  **or**
* Use **Manual Input** to connect via IPv4 or IPv6 address

> ⚠️ Loopback address (`127.0.0.1`) is strictly prohibited by project rules.

---

## Supported Commands

| Command                    | Description                 |
| -------------------------- | --------------------------- |
| LOOK                       | View current room           |
| INVENTORY                  | Show owned items            |
| MOVE North/South/East/West | Move between rooms          |
| TAKE item                  | Pick up an item             |
| DEPOSIT item               | Drop an item                |
| GIVE player item           | Give item to another player |
| TELL player message        | Private message             |
| SAVE                       | Save game state             |
| SAVE_EXIT                  | Save and exit               |
| EXIT                       | Exit without saving         |

Commands can be issued via **GUI buttons** or **command line input**.

---

## Testing IPv6 Connectivity

Check IPv6 availability:

```bash
ip -6 addr
```

Connect using IPv6 address in the client:

```
fe80::xxxx:xxxx:xxxx:xxxx%interface
```

The system automatically selects IPv4 or IPv6 using `getaddrinfo(AF_UNSPEC)`.

---

