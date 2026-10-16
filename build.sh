#!/bin/bash
# 视频工具箱 —— 一键构建 .fpk
# 用法：bash build.sh [--skip-ffmpeg] [--cpu-variant]
#   --skip-ffmpeg  已有 app/bin/ffmpeg 时跳过下载
#   --cpu-variant  额外产出不含 video/render 附加组的降级包（目标机缺少这些系统组时使用）
#
# 打包机制说明（踩过的坑，勿删）：
#   fnpack 会把「打包源目录」整体复制到 $TMPDIR/fnpack.<pid>/ 之后才开始压缩。
#   1) $TMPDIR 若位于打包源目录内部 → 复制会把自己再复制一遍，无限递归，
#      实测膨胀到 8G+ 后报 "no such file or directory" 而失败。
#   2) fnpack 不读取 .gitignore，源目录里的一切都会被复制（build/cache 有 200MB 以上），
#      所以不能直接以项目根目录为源目录，否则会把缓存、测试、临时文件统统打进包里。
#   因此这里：① 先把「该进包的文件」复制到干净目录 build/stage/；
#            ② 再用 `fnpack build -d build/stage` 打包；
#            ③ $TMPDIR 指向项目根目录之外的大容量分区。

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
CACHE="${ROOT}/build/cache"
BIN="${ROOT}/app/bin"
DIST="${ROOT}/dist"
STAGE="${ROOT}/build/stage"
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
FNPACK_URL="https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64"

SKIP_FFMPEG=0
CPU_VARIANT=0
for arg in "$@"; do
    case "${arg}" in
    --skip-ffmpeg) SKIP_FFMPEG=1 ;;
    --cpu-variant) CPU_VARIANT=1 ;;
    *) echo "未知参数：${arg}" >&2; exit 1 ;;
    esac
done

mkdir -p "${CACHE}" "${BIN}" "${DIST}"

WORKSPACE="$(cd "${ROOT}/.." && pwd)"

# 选择打包临时目录：必须在项目根目录之外，且容量足够（内置 ffmpeg 约 160MB，
# 复制时会临时占用同等空间）。某些环境 /tmp 是 10MB 的 tmpfs，必须跳过。
# 可用 VTOOLBOX_BUILD_TMP 显式指定。
BUILD_TMP=""
for candidate in "${VTOOLBOX_BUILD_TMP:-}" \
                 "${WORKSPACE}/.workbuddy/tmp/fnpack" \
                 "${WORKSPACE}/../.workbuddy/tmp/fnpack" \
                 "${HOME:-/root}/.cache/vtoolbox-build" \
                 "/var/tmp/vtoolbox-build" \
                 "${TMPDIR:-/tmp}"; do
    [ -n "${candidate}" ] || continue
    # 与项目根目录重叠的候选一律丢弃，避免 fnpack 自我递归
    case "${candidate}" in
    "${ROOT}" | "${ROOT}"/*) continue ;;
    esac
    if mkdir -p "${candidate}" 2>/dev/null && [ -w "${candidate}" ]; then
        BUILD_TMP="${candidate}"
        break
    fi
done
if [ -n "${BUILD_TMP}" ]; then
    export TMPDIR="${BUILD_TMP}"
    echo "打包临时目录：${TMPDIR}"
else
    echo "警告：未找到可写的打包临时目录，fnpack 可能因空间不足失败" >&2
fi

PY="$(command -v python3 || echo /usr/bin/python3)"

echo "== 1/6 准备内置 ffmpeg =="
if [ -x "${BIN}/ffmpeg" ] && [ -x "${BIN}/ffprobe" ]; then
    echo "已存在，跳过下载（删除 app/bin/ffmpeg 可重新下载）"
elif [ "${SKIP_FFMPEG}" = "1" ]; then
    echo "跳过下载：请自行把 linux-amd64 静态 ffmpeg / ffprobe 放进 app/bin/"
else
    if [ ! -s "${CACHE}/ffmpeg.tar.xz" ]; then
        echo "下载静态 ffmpeg（约 40MB，首次构建需要科学网络环境）…"
        curl -fL --progress-bar -o "${CACHE}/ffmpeg.tar.xz" "${FFMPEG_URL}"
    fi
    rm -rf "${CACHE}/extract"
    mkdir -p "${CACHE}/extract"
    tar -xJf "${CACHE}/ffmpeg.tar.xz" -C "${CACHE}/extract" --strip-components=1
    cp "${CACHE}/extract/ffmpeg" "${BIN}/ffmpeg"
    cp "${CACHE}/extract/ffprobe" "${BIN}/ffprobe"
fi
chmod +x "${BIN}/ffmpeg" "${BIN}/ffprobe" 2>/dev/null || true

echo "== 2/6 生成图标 =="
"${PY}" "${ROOT}/build/gen_icons.py" "${ROOT}"

echo "== 3/6 设置脚本权限 =="
chmod +x "${ROOT}"/cmd/*

echo "== 4/6 校验 python 源码 =="
PYTHONPYCACHEPREFIX="${CACHE}/pycache" "${PY}" -m py_compile \
    "${ROOT}/app/server/main.py" "${ROOT}/app/server/media.py" "${ROOT}/app/server/jobs.py"
echo "语法检查通过"

echo "== 5/6 准备干净打包目录 =="
# 清理源码树里的临时文件（pyc/pycache），再复制到 staging
find "${ROOT}/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${ROOT}/app" -name '*.pyc' -delete 2>/dev/null || true

rm -rf "${STAGE}"
mkdir -p "${STAGE}"
# 复制时不让它保留时间戳：部分沙箱/受限环境会间歇性禁止设置文件时间（utimensat EPERM），
# 报 "保留 … 的时间: 不允许的操作"，并且 cp 会以非 0 退出，在 set -e 下直接中断构建。
# 这里双保险：优先 --no-preserve=timestamps，失败再退回普通复制并忽略退出码，
# 最后用「文件数比对」来确认复制是否真的完整。
for item in manifest ICON.PNG ICON_256.PNG README.md app cmd config wizard; do
    if [ -e "${ROOT}/${item}" ]; then
        cp -r --no-preserve=timestamps "${ROOT}/${item}" "${STAGE}/" 2>/dev/null \
            || cp -r "${ROOT}/${item}" "${STAGE}/" 2>/dev/null \
            || true
    fi
done
chmod +x "${STAGE}"/cmd/* 2>/dev/null || true
chmod +x "${STAGE}/app/bin/ffmpeg" "${STAGE}/app/bin/ffprobe" 2>/dev/null || true

# 确认关键内容确实到位（cp 报时间戳错误不代表文件没写进去，所以看实际结果而不是退出码）
for need in manifest app cmd config wizard ICON.PNG ICON_256.PNG; do
    [ -e "${STAGE}/${need}" ] || { echo "打包失败：暂存目录缺少 ${need}" >&2; exit 1; }
done
SRC_N="$(find "${ROOT}/app" -type f | wc -l)"
DST_N="$(find "${STAGE}/app" -type f | wc -l)"
if [ "${SRC_N}" != "${DST_N}" ]; then
    echo "打包失败：暂存目录 app 复制不完整（${DST_N}/${SRC_N} 个文件）" >&2
    exit 1
fi
echo "打包内容：$(ls -1 "${STAGE}" | tr '\n' ' ') （app ${DST_N} 个文件）"

echo "== 6/6 打包 .fpk =="
FNPACK="${FNPACK:-${CACHE}/fnpack}"
if [ ! -x "${FNPACK}" ]; then
    echo "下载官方打包工具 fnpack …"
    curl -fL -o "${FNPACK}" "${FNPACK_URL}"
    chmod +x "${FNPACK}"
fi

# 注意：fnpack 把 .fpk 写到「当前工作目录」，`-d/--directory` 只指定源目录，
# 所以这里直接 cd 进 staging 再 build，产物才会落在 ${STAGE} 下。
(cd "${STAGE}" && "${FNPACK}" build)

PKG="$(ls -1t "${STAGE}"/*.fpk 2>/dev/null | head -n 1)"
if [ -z "${PKG}" ]; then
    echo "打包失败：未生成 .fpk" >&2
    exit 1
fi
cp -f "${PKG}" "${DIST}/"

# 额外按版本号命名一份，便于区分不同版本
VER="$(grep -E '^version' "${ROOT}/manifest" | head -n 1 | cut -d= -f2 | tr -d ' \t')"
if [ -n "${VER}" ]; then
    cp -f "${PKG}" "${DIST}/vtoolbox-${VER}.fpk"
fi

# 降级包：去掉 join-groups，避免个别机器缺少 video/render 系统组时安装失败
if [ "${CPU_VARIANT}" = "1" ]; then
    echo "== 额外产出降级包（不含 join-groups） =="
    cat > "${STAGE}/config/privilege" <<'JSON'
{
    "defaults":
    {
        "run-as": "package"
    },
    "username": "vtoolbox",
    "groupname": "vtoolbox"
}
JSON
    (cd "${STAGE}" && "${FNPACK}" build) >/dev/null
    PKG2="$(ls -1t "${STAGE}"/*.fpk 2>/dev/null | head -n 1)"
    if [ -n "${PKG2}" ]; then
        cp -f "${PKG2}" "${DIST}/vtoolbox-${VER}-cpu.fpk"
    else
        echo "警告：降级包未生成" >&2
    fi
    # staging 里的 privilege 需要还原，否则下次打包会误用降级配置
    cp -f "${ROOT}/config/privilege" "${STAGE}/config/privilege"
fi

echo
echo "构建完成：${DIST}/vtoolbox-${VER:-unknown}.fpk  （版本 ${VER:-未知}）"
ls -lh "${DIST}/"
