FROM hdfgroup/python:3.7
MAINTAINER John Readey <jreadey@hdfgroup.org>
RUN pip install azure-storage-blob
RUN pip install aiofiles
RUN pip install aiohttp-cors
RUN pip install pyjwt
RUN mkdir /usr/local/src/hsds/ /usr/local/src/tests/
COPY hsds /usr/local/src/hsds/
COPY admin/config/passwd.txt /usr/local/src/hsds/
COPY tests /usr/local/src/tests/
COPY testall.py /usr/local/src/
COPY entrypoint.sh  /

EXPOSE 5100-5999
 
ENTRYPOINT ["/entrypoint.sh"]