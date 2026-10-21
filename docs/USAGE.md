# 使用教程 —— Linux ↔ ColorOS / realme 手机互传

本教程基于 **realme GT7 Pro（ColorOS 17 / Android 17）** 实测，
电脑为 CachyOS（Arch 系）。全程**不修改手机**、**不需要手机 root**。

链路是这样走的（每一段都有实测证据）：

```
BLE 广播 0x8881 + 0x6666/0x6667
   ↓ 手机「其他设备」里出现 LinuxBox
GATT 连接，读我们 0x9996（五字段 + EC 公钥）
   ↓ 手机把 300 字节分享请求写到我们 0x9985
我们反连手机：读 0x9954（置 transferType=2）→ 写 0x9953（port/SSID/PSK JSON）
   ↓ 手机据此 P2P 连接我们建的 Wi-Fi Direct GO
关联 + 4 次握手 + DHCP（手机拿到 192.168.49.x）
   ↓
wss://192.168.49.1:<port>/websocket
   action versionNegotiation ⇄ ack
   action sendRequest        ⇄ ack
   GET /download?taskId=…    → 分块 ZIP
   action status{"type":1}
   ↓
手机 Documents/realme Share/ 出现文件
```

---

## 1. 准备

### 1.1 系统依赖

```sh
# CachyOS / Arch
sudo pacman -S bluez bluez-utils dnsmasq python-gobject iptables
sudo systemctl enable --now bluetooth

# Debian / Ubuntu
sudo apt install bluez bluez-tools dnsmasq python3-gi iptables
sudo systemctl enable --now bluetooth
```

只需要 **python-gobject**（`Gio`/`GLib`），**不需要** `python-dbus`。

### 1.2 检查网卡支持

```sh
iw list | sed -n '/Supported interface modes/,/^$/p'
```

必须包含 `* P2P-GO`（本项目用电脑当 Wi-Fi Direct 组主）。绝大多数
Intel / MediaTek / Qualcomm 网卡都支持。

### 1.3 构建 Go 部分（可选）

教程里的手机互通不依赖 Go 二进制，但协议库本身仍然可以单独使用：

```sh
go build -o oshare ./cmd/oshare
go test ./...
```

---

## 2. 发文件给手机（推荐：一条命令）

```sh
sudo ./tools/oshare-go.sh -f ~/Pictures/photo.jpg
```

然后**在手机上**：

1. 打开「互传」（realme / OPPO 手机自带）
2. 进入「发送」页
3. 选一个文件
4. 在「其他设备」里点 **LinuxBox**

**结束。** 文件会出现在手机的 `Documents/realme Share/`。

电脑端你会看到类似输出：

```
[16:07:03] unmanaging wlan0
[16:07:09] group owner on p2p-wlan0-0
[16:07:09] serving photo.jpg on wss://192.168.49.1:50856/websocket
[16:07:11] starting BLE peer (advertising as LinuxBox)
[16:07:11] ready - open 互传 on the phone, pick a file, tap LinuxBox
[16:07:59] phone accepted, serving the file ...
[16:08:01] transfer complete
```

### 参数

| 参数 | 说明 |
|---|---|
| `-f, --file PATH` | 要发送的文件；不填则发一个内置小文件 |
| `-p, --port N` | 指定端口；不填自动挑一个空闲端口 |
| `-n, --name NAME` | 手机里显示的设备名（默认 `LinuxBox`） |
| `-s, --ssid S` | 建的热点名（默认 `DIRECT-aa-fastCon`） |
| `-k, --psk P` | 热点密码；不填自动生成 8 位数字 |

### 收尾

`Ctrl-C` 会：拆掉 Wi-Fi Direct 组、停掉 DHCP 与 WebSocket 服务、
把 `wlan0` 还给 NetworkManager、恢复代理。

---

## 3. 从手机收文件

手机当发送方时，它自己是组主，所以先加入它建的组：

```sh
# 终端 1（需要 root）：自动从手机读当前 PSK 并加入
sudo ./tools/p2p_join.sh

# 终端 2：拉取
python3 tools/oshare_recv.py --host 192.168.49.1 --dir ./received
```

`p2p_join.sh` 会打印手机是否可达；`oshare_recv.py` 会：

1. 连 `wss://<phone>:<port>/websocket`
2. 回 `versionNegotiation` 与 `sendRequest` 的 ack
3. `GET /download?taskId=…` 并解分块
4. 把 ZIP 解到 `--dir`，重名自动加 `-1`、`-2`

**端口问题**：手机只通过 BLE 通知（`0x9989`）告诉对端端口，而这条通知
在 BlueZ 上收不到（详见第 6 节）。所以：

* 知道端口就 `--port 50856` 直接指定；
* 不知道就不填，脚本会扫 `40000-65535` 找能回 WebSocket 101 的端口。

---

## 4. 分步使用（调试用）

一键脚本的每一步都能单独跑：

### 4.1 建 Wi-Fi Direct 组主

```sh
sudo ./tools/p2p_go.sh "DIRECT-aa-fastCon" "12345678" 50856
```

它做四件事：接管 `wlan0` → 建持久 P2P 组 →
`192.168.49.1/24` + DHCP（`.10-.200`）→ 放行防火墙 →
在 50856 起 `wss://` 服务。

### 4.2 广播 + GATT 服务

```sh
# 只看附近的广播（调试用，会高亮 OShare 设备）
python3 tools/oshare_ble.py scan -t 20

# 广播并托管 GATT 服务；--share-* 让它在手机连上来后自动推 AP 信息
python3 tools/oshare_ble.py serve --name LinuxBox --no-ack \
        --share-port 50856 --share-ssid DIRECT-aa-fastCon --share-psk 12345678
```

`--share-port` 一旦给出，手机写来分享请求后它会自动执行第 4.4 步。

### 4.3 只要服务端

```sh
python3 tools/oshare_ws.py --port 50856 --file ~/movie.mp4
```

日志写在 `/tmp/oshare-ws.log`，`tail -f` 就能看手机的每一条消息。

### 4.4 只推 AP 信息（手动）

```sh
python3 tools/push_json.py --match 真我 --port 50856 \
        --ssid "DIRECT-aa-fastCon" --psk "12345678" --freq 2437
```

`--match` 可以是手机名的一部分或 MAC 前缀。`--mac` 默认取
**P2P-device 地址**（`wlan0` MAC 把第一字节的本地管理位打开，
即 `4c:…` → `4e:…`），这个必须对，否则手机会找不到我们。

### 4.5 诊断

```sh
# 手机上互传的日志（能看到它每一步在等什么）
adb shell su -c 'tail -f /data/media/0/Android/data/com.coloros.oshare/files/logs/oshare.log'

# 我们的 BLE 服务输出
tail -f /tmp/oshare-ble.out
```

---

## 5. 常见问题

### 手机「其他设备」里看不到 LinuxBox

1. 确认广播在跑：`python3 tools/oshare_ble.py scan -t 10` 应该能看到自己的
   `LinuxBox`（MAC 是本机蓝牙地址）。
2. **广播必须 ≤31 字节**。超出后 BlueZ 会改用扩展广播，而手机的扫描器
   只认传统广播，于是完全看不到。本项目已经把 `0x6666`/`0x6667` 拆到
   ADV_IND 和 SCAN_RSP 里，不要自己往广播里加名字或 UUID。
3. 手机缓存了旧的 GATT 结构时也会异常：

   ```sh
   adb shell su -c 'rm -f /data/misc/bluetooth/gatt_*'
   adb shell su -c 'svc bluetooth disable; sleep 2; svc bluetooth enable'
   ```

### 手机点了之后提示「发送超时，对方未确认」

正常流程里这一步**不需要**我们回任何通知（见第 6 节）。出现这个提示说明
手机写来的分享请求已经收到了，但我们没能反连成功：

* 看 `/tmp/oshare-ble.out` 有没有 `[share] writing 0x9953`；
* 没有的话，多半是手机 P2P 随机 MAC 变了，重新跑一次一键脚本即可
  （它每次都会重新发现设备）。

### 手机连上了但文件没出现

* 手机的保存路径是 `Documents/realme Share/`（老版本是
  `Download/realme Share/`）；
* 看 `/tmp/oshare-ws.log`：如果出现 `sent … for /download`，说明数据已经
  发出去了，问题在手机解压；如果连 `phone accepted` 都没有，说明
  `sendRequest` 没被接受（`totalSize` 与 `fileCount` 要和实际一致）。

### 电脑自己断网了

一键脚本会接管 `wlan0` 并停掉 TUN 代理。如果电脑还有别的网口
（有线 / USB 共享网络），上网不受影响；否则测试期间会短暂断网，
`Ctrl-C` 或脚本结束就会恢复。

---

## 6. 为什么不需要修 BlueZ（原理）

网上常见说法是「要在 Linux 上互传，必须让 BlueZ 能发 GATT 通知」。
本项目**完全没有用到通知**：

* 手机把 `0x9989` / `0x9985` 声明成 `read|write`（没有 notify 属性），
  所以对它们 `StartNotify` 会被 BlueZ 直接拒（`NotSupported`）；
* 手机自己也不写 CCC 描述符（`setCharacteristicNotification()` 只在本地置位），
  所以即使属性正确，BlueZ 也不会发出通知。

于是握手改成**纯读写**：

| 步骤 | 特征 | 方向 | 作用 |
|---|---|---|---|
| 1 | `0x9996` | 手机读我们 | 我们的 Wi-Fi MAC + EC 公钥 |
| 2 | `0x9985` | 手机写我们 | 分享请求（文件名/大小/MIME） |
| 3 | `0x9954` | 我们读手机 | 让手机把 `transferType` 置 2 |
| 4 | `0x9953` | 我们写手机 | `{port, mac, ssid, psk}`，手机据此连我们 |

第 3、4 步是**关键**：只有先读 `0x9954`，手机才接受 `0x9953` 的写入；
写入成功后它直接走 `HttpP2pClient.connectToServer`，自己连到我们给的端口。

详见 [TO-PHONE.md](TO-PHONE.md)（逐步实测记录）与
[BLE.md](BLE.md)（广播与 GATT 结构）。

---

## 7. 已知限制

* **只支持 Wi-Fi Direct 这条联盟路线**。OPPO 的 PantaConnect（账号配对）
  路线需要 TEE + 厂商证书，无法在普通电脑上实现，见 [HANDOFF.md](HANDOFF.md)。
* 工具目前是 Python 原型；Go 侧有完整的帧协议与 TLS 传输库，
  但 `wss://` 控制通道尚未移植。
* 手机每次建组都会换 SSID 后缀与 PSK，脚本每次都会重新读取。
* 反方向（手机 → 电脑）的端口发现仍依赖扫描，因为那条通知收不到。
