/* Local DVD navigation and bitmap decoding, using FFmpeg's dvdvideo demuxer.
 * Build with setup_dvd.py. No video/audio transcoding or network input.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <inttypes.h>
#include <limits.h>
#include <string.h>
#include <stdarg.h>
#include <dvdread/dvd_reader.h>
#include <dvdread/ifo_read.h>
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>

static int decoding=0, decode_error=0;
static void logging(void *context, int level, const char *format, va_list args) {
    if (decoding && level<=AV_LOG_ERROR) decode_error=1;
    av_log_default_callback(context,level,format,args);
}

static void fail(const char *message) {
    fprintf(stderr, "ERROR: %s\n", message); exit(1);
}
static int number(const char *s, int maximum) {
    char *end; long n = strtol(s, &end, 10);
    if (!*s || *end || n < 0 || n > maximum) fail("Invalid numeric argument.");
    return n;
}
static AVFormatContext *open_title(const char *path, int title) {
    AVFormatContext *f = NULL; AVDictionary *opts = NULL;
    av_dict_set_int(&opts, "title", title, 0);
    av_dict_set(&opts, "trim", "0", 0); /* Keep VLC's complete title timeline. */
    int ret = avformat_open_input(&f, path, av_find_input_format("dvdvideo"), &opts);
    av_dict_free(&opts);
    if (ret < 0) return NULL;
    return f;
}
static void scan(const char *path, const char *out) {
    dvd_reader_t *dvd = DVDOpen(path);
    if (!dvd) fail("Could not open the local DVD folder.");
    ifo_handle_t *ifo = ifoOpen(dvd, 0);
    if (!ifo || !ifo->tt_srpt) fail("DVD title table is missing.");
    int count = ifo->tt_srpt->nr_of_srpts;
    ifoClose(ifo); DVDClose(dvd);
    if (count < 1 || count > 99) fail("Invalid DVD title count.");
    FILE *fp = fopen(out, "w"); if (!fp) fail("Could not write DVD discovery.");
    fprintf(fp, "{\"version\":1,\"titles\":[");
    for (int title = 1; title <= count; title++) {
        if (title > 1) fputc(',', fp);
        AVFormatContext *f = open_title(path, title);
        fprintf(fp, "{\"title\":%d", title);
        if (!f) { fprintf(fp, ",\"error\":\"Cannot navigate this DVD title\"}"); continue; }
        int width=0, height=0, first=1;
        for (unsigned i=0; i<f->nb_streams; i++) {
            AVCodecParameters *p=f->streams[i]->codecpar;
            if (p->codec_type==AVMEDIA_TYPE_VIDEO) { width=p->width; height=p->height; break; }
        }
        fprintf(fp, ",\"duration\":%.3f,\"width\":%d,\"height\":%d,\"streams\":[",
                f->duration > 0 ? f->duration/(double)AV_TIME_BASE : 0, width, height);
        for (unsigned i=0; i<f->nb_streams; i++) {
            AVStream *s=f->streams[i];
            if (s->codecpar->codec_id != AV_CODEC_ID_DVD_SUBTITLE) continue;
            AVDictionaryEntry *lang=av_dict_get(s->metadata,"language",NULL,0);
            const char *language=lang ? lang->value : "und";
            if (strlen(language)>3 || strspn(language,"abcdefghijklmnopqrstuvwxyz")!=strlen(language)) language="und";
            fprintf(fp, "%s{\"index\":%u,\"id\":%d,\"language\":\"%s\"}", first?"":",", i,s->id,language);
            first=0;
        }
        fprintf(fp, "]}"); avformat_close_input(&f);
    }
    fprintf(fp, "]}\n"); if (fclose(fp)) fail("Could not finish DVD discovery.");
}
static void extract(const char *path, int title, int index, const char *out) {
    AVFormatContext *f=open_title(path,title);
    if (!f) fail("Could not navigate the selected DVD title.");
    if ((unsigned)index>=f->nb_streams || f->streams[index]->codecpar->codec_id!=AV_CODEC_ID_DVD_SUBTITLE)
        fail("Selected DVD subtitle track does not exist.");
    int width=0,height=0;
    for (unsigned i=0;i<f->nb_streams;i++) if (f->streams[i]->codecpar->codec_type==AVMEDIA_TYPE_VIDEO) {
        width=f->streams[i]->codecpar->width; height=f->streams[i]->codecpar->height; break;
    }
    if (width<1 || width>4096 || height<1 || height>2160) fail("Invalid DVD canvas.");
    AVStream *stream=f->streams[index];
    int64_t max_time=f->duration>0 ? av_rescale_q(f->duration,AV_TIME_BASE_Q,(AVRational){1,90000})+450000 : INT64_MAX;
    const AVCodec *codec=avcodec_find_decoder(AV_CODEC_ID_DVD_SUBTITLE);
    AVCodecContext *ctx=avcodec_alloc_context3(codec);
    if (!ctx || avcodec_parameters_to_context(ctx,stream->codecpar)<0) fail("Could not prepare DVD decoder.");
    ctx->pkt_timebase=stream->time_base;
    ctx->err_recognition=AV_EF_EXPLODE;
    if (avcodec_open2(ctx,codec,NULL)<0) fail("Could not open DVD decoder.");
    char file[PATH_MAX];
    if (snprintf(file,sizeof(file),"%s/displays.json",out)>=(int)sizeof(file)) fail("Output path is too long.");
    FILE *manifest=fopen(file,"w"); if (!manifest) fail("Could not write DVD manifest.");
    fprintf(manifest,"{\"version\":1,\"title\":%d,\"index\":%d,\"width\":%d,\"height\":%d,\"displays\":[",title,index,width,height);
    AVPacket *pkt=av_packet_alloc(); int ret,count=0; int64_t previous=-1, total_pixels=0;
    while ((ret=av_read_frame(f,pkt))>=0) {
        if (pkt->stream_index==index) {
            if (pkt->pts==AV_NOPTS_VALUE) fail("DVD subtitle has no timestamp.");
            if (pkt->size<4 || pkt->size!=((pkt->data[0]<<8)|pkt->data[1])) fail("Truncated DVD subtitle packet.");
            AVSubtitle sub={0}; int got=0;
            decoding=1; decode_error=0;
            int used=avcodec_decode_subtitle2(ctx,&sub,&got,pkt);
            decoding=0;
            if (decode_error || used!=pkt->size) fail("Invalid or incomplete DVD subtitle packet.");
            if (got && sub.num_rects) {
                int64_t base=av_rescale_q(pkt->pts,stream->time_base,(AVRational){1,90000});
                int64_t start=base+sub.start_display_time*90LL, end=base+sub.end_display_time*90LL;
                if (start<0 || end<=start || start<previous || end>max_time || end-start>90000LL*600) fail("DVD subtitle has invalid or missing end time, or exceeds the title duration.");
                if (++count>100000) fail("Too many DVD subtitles.");
                previous=start;
                fprintf(manifest,"%s{\"start\":%"PRId64",\"end\":%"PRId64",\"parts\":[",count>1?",":"",start,end);
                for (unsigned j=0;j<sub.num_rects;j++) {
                    AVSubtitleRect *r=sub.rects[j];
                    if (r->type!=SUBTITLE_BITMAP || r->w<=0 || r->h<=0 || r->x<0 || r->y<0 || r->x+r->w>width || r->y+r->h>height || !r->data[0] || !r->data[1]) fail("Invalid DVD subtitle bitmap.");
                    total_pixels+=(int64_t)r->w*r->h;
                    if (total_pixels>256000000) fail("DVD subtitle images exceed the 256 MB extraction limit.");
                    char name[64]; snprintf(name,sizeof(name),"%d-%u.gray",count,j);
                    if (snprintf(file,sizeof(file),"%s/%s",out,name)>=(int)sizeof(file)) fail("Output path is too long.");
                    FILE *image=fopen(file,"wb"); if (!image) fail("Could not write subtitle image.");
                    uint32_t *palette=(uint32_t *)r->data[1];
                    for (int y=0;y<r->h;y++) for (int x=0;x<r->w;x++) {
                        unsigned color=r->data[0][y*r->linesize[0]+x];
                        if (color>=(unsigned)r->nb_colors) fail("Invalid DVD palette index.");
                        uint32_t rgba=palette[color];
                        int lum=(299*((rgba>>16)&255)+587*((rgba>>8)&255)+114*(rgba&255)+500)/1000;
                        if (fputc(255-(lum*(rgba>>24)+127)/255,image)==EOF) fail("Could not write subtitle pixels.");
                    }
                    if (fclose(image)) fail("Could not finish subtitle image.");
                    fprintf(manifest,"%s{\"x\":%d,\"y\":%d,\"width\":%d,\"height\":%d,\"file\":\"%s\",\"forced\":%s}",j?",":"",r->x,r->y,r->w,r->h,name,(r->flags&AV_SUBTITLE_FLAG_FORCED)?"true":"false");
                }
                fprintf(manifest,"]}");
            }
            avsubtitle_free(&sub);
        }
        av_packet_unref(pkt);
    }
    if (ret!=AVERROR_EOF) fail("DVD reading stopped before the end of the selected title.");
    fprintf(manifest,"]}\n"); if (fclose(manifest)) fail("Could not finish DVD manifest.");
    av_packet_free(&pkt); avcodec_free_context(&ctx); avformat_close_input(&f);
    if (!count) fail("No visible subtitles in the selected DVD track.");
}
int main(int argc,char **argv) {
    av_log_set_level(AV_LOG_WARNING);
    av_log_set_callback(logging);
    if (argc==4 && !strcmp(argv[1],"scan")) scan(argv[2],argv[3]);
    else if (argc==6 && !strcmp(argv[1],"extract")) extract(argv[2],number(argv[3],99),number(argv[4],255),argv[5]);
    else fail("Usage: dvd-subtitles scan PATH JSON | extract PATH TITLE INDEX DIRECTORY");
    return 0;
}
