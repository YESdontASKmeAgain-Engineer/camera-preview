# Orange Pi Camera 推流命令手册

适用设备：Orange Pi CM4  
当前局域网地址：`192.168.10.143`  
项目目录：`/home/orangepi/CameraPreview`

## 1. 启动推流

建议在香橙派远程桌面的终端中执行：

```bash
cd /home/orangepi/CameraPreview
./run-camera-preview.sh --camera 0 --width 320 --height 240 --fps 5 --start-lan-stream
```

第一个终端会被 Camera 程序占用。需要执行其他命令时，在 Tabby 中按
`Ctrl+Shift+T` 新建标签页，再连接同一台香橙派。

当前设备曾在较高负载下异常重启，建议先使用 `5 FPS`。确认供电和散热稳定后，
可以将 `--fps 5` 改为 `--fps 10` 测试，不建议直接使用 30 FPS。

## 2. 检查是否正在推流

```bash
ss -ltnp | grep 8080
```

出现类似下面的结果表示推流服务已经启动：

```text
LISTEN 0 5 0.0.0.0:8080 0.0.0.0:* users:(("python3",pid=7667,fd=5))
```

查看 Camera 进程及其启动参数：

```bash
ps -ef | grep '[c]amera_preview.py'
```

## 3. 显示推流地址

显示单路 MJPEG 拉流地址：

```bash
echo "http://$(hostname -I | awk '{print $1}'):8080/stream/0-b6589fc6.mjpg"
```

显示浏览器预览主页：

```bash
echo "http://$(hostname -I | awk '{print $1}'):8080/"
```

当前地址为：

```text
http://192.168.10.143:8080/stream/0-b6589fc6.mjpg
```

认证信息：

- 用户名：`camera`
- 密码：在 Camera 的“局域网推流”设置中填写的密码

程序的视频流地址框只填写完整的 `http://...` 地址，不要填写 `echo`、`$()`、
引号或 `awk` 命令。

## 4. 从 Windows 检查连接

在 Windows PowerShell 或 Tabby 的本地 PowerShell 中执行：

```powershell
curl.exe -i -L http://192.168.10.143:8080/
```

返回 `401 Password required` 表示电脑已经成功连接香橙派，只是还需要用户名和
密码。浏览器打开推流地址并使用用户名 `camera` 登录后，能持续看到变化的画面，
表示推流和拉流都正常。

## 5. 停止推流

如果 Camera 正在第一个终端前台运行，在该终端按：

```text
Ctrl+C
```

也可以先查询进程号：

```bash
pgrep -af camera_preview.py
```

然后将 `<PID>` 替换为查询到的数字：

```bash
kill <PID>
```

再次检查端口。没有输出就表示已经停止：

```bash
ss -ltnp | grep 8080
```

## 6. 查看系统状态

实时查看 CPU、内存和进程：

```bash
htop
```

如果没有安装 `htop`，也可以使用：

```bash
top
```

查看内存：

```bash
free -h
```

查看磁盘：

```bash
df -h
```

查看系统负载和运行时间：

```bash
uptime
```

查看 CPU 温度，结果需要除以 1000：

```bash
cat /sys/class/thermal/thermal_zone0/temp
```

例如 `66000` 表示约 `66 C`。当前系统从 `75 C` 开始被动降频，因此持续推流时
需要留意供电和散热。

## 7. 常见输入错误

宽高参数和值之间必须有空格：

```text
正确：--height 240
错误：--height240
```

检查端口时，冒号和端口号之间不能有空格：

```text
正确：ss -ltnp 'sport = :8080'
错误：ss -ltnp 'sport = : 8080'
```

推流用户名是 `camera`，不是 `camer`。
