from mojo.helpers.response import JsonResponse
from mojo.apps.mailman import utils
from mojo.apps.mailman.models import Message
import requests


def on_confrim_subscription(request):
    url = request.DATA.get("SubscribeURL", None)
    resp = requests.get(url)
    # TODO record this as an event/incident
    return JsonResponse(dict(status=True))


def on_message(request):
    """
    Email receiving can be configured in 2 ways.
     1. raw email via SNS which would have content field
     2. s3 bucket stored email which will have a receipt.action.bucketName
    """
    msg = request.DATA
    if msg.content is None and msg.receipt and msg.receipt.action:
        action = msg.receipt.action
        if action.type == "S3":
            return on_s3_email(request, msg, action.bucketName, action.objectKey)

    if msg.content is None:
        logger.error("message has no content", msg)
        return

    on_raw_email(utils.parse_raw_message(msg.content))
    return


def on_s3_email(request, msg, bucket_name, object_key):
    msg_data = utils.parse_raw_message(s3store.getObjectContent(bucket_name, object_key))
    return on_raw_email(request, msg, msg_data)


def on_raw_email(request, imsg, msg_data):
    to_email = imsg.receipt.recipients[0]
    # logger.info("parsed", msg_data)
    msg = createMessage(to_email, msg_data)
    metrics.metric("emails_received", category="email", min_granularity="hours")

    attachments = []
    for msg_atch in msg_data.attachments:
        atch = Attachment(message=msg, name=msg_atch.name, content_type=msg_atch.content_type)
        if msg_atch.encoding == "base64":
            atch.saveMediaFile(msg_atch.payload, "media", msg_atch.name, is_base64=True)
        elif msg_atch.encoding == "quoted-printable":
            obj = mailtils.toFileObject(msg_atch)
            atch.saveMediaFile(obj, "media", msg_atch.name)
        else:
            logger.error("unknown encoding", msg_atch.encoding)
            continue
        atch.save()
        attachments.append(atch)
    # add the recipients mailbox
    addToMailbox(msg)
    # now lets check if we have more recipients
    for to_email in imsg.receipt.recipients[1:]:
        msg = createMessage(to_email, msg_data)
        for atch in attachments:
            # create a copy of the attachment, for the new msg
            atch.pk = None
            atch.message = msg
            atch.save()
        addToMailbox(msg)
    return rv.restStatus(request, True)



def create_message(to_email, msg_data):
    msg = Message(
        to_email=to_email,
        sent_at=msg_data.sent_at,
        subject=msg_data.subject,
        message=msg_data.message,
        html=msg_data.html,
        body=msg_data.body,
        to=msg_data.to,
        cc=msg_data.cc,
        from_email=msg_data.from_email,
        from_name=msg_data.from_name)
    msg.save()
    return msg
